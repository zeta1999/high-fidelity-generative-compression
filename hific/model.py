"""
Stitches submodels together.
"""
import numpy as np
import time, os
import itertools

from collections import defaultdict, namedtuple

import torch
import torch.nn as nn
import torch.nn.functional as F

# Custom modules
import hific.perceptual_similarity as ps
from hific.submodels import network, hyperprior
from hific.utils import helpers, datasets, math, losses

from default_config import ModelModes, ModelTypes, hific_args, directories

Intermediates = namedtuple("Intermediates",
    ["input_image",             # [0, 1] (after scaling from [0, 255])
     "reconstruction",          # [0, 1]
     "latents_quantized",        # Latents post-quantization.
     "n_bpp",                   # Differential entropy estimate.
     "q_bpp"])                  # Shannon entropy estimate.

Disc_out= namedtuple("disc_out",
    ["D_real", "D_gen", "D_real_logits", "D_gen_logits"])

class HificModel(nn.Module):

    def __init__(self, args, logger, storage_train, storage_test, model_mode=ModelModes.TRAINING, 
            model_type=ModelTypes.COMPRESSION):
        super(HificModel, self).__init__()

        """
        Builds hific model from submodels.
        """
        self.args = args
        self.model_mode = model_mode
        self.model_type = model_type
        self.logger = logger
        self.log_interval = args.log_interval
        self.storage_train = storage_train
        self.storage_test = storage_test
        self.step_counter = 0

        if not hasattr(ModelTypes, self.model_type.upper()):
            raise ValueError("Invalid model_type: [{}]".format(self.model_type))
        if not hasattr(ModelModes, self.model_mode.upper()):
            raise ValueError("Invalid model_mode: [{}]".format(self.model_mode))

        self.image_dims = self.args.image_dims  # Assign from dataloader
        self.batch_size = self.args.batch_size

        self.Encoder = network.Encoder(self.image_dims, self.batch_size, C=self.args.latent_channels,
            channel_norm=self.args.use_channel_norm)
        self.Generator = network.Generator(self.image_dims, self.batch_size, C=self.args.latent_channels,
            n_residual_blocks=self.args.n_residual_blocks, channel_norm=self.args.use_channel_norm)

        self.Hyperprior = hyperprior.Hyperprior(bottleneck_capacity=self.args.latent_channels)

        self.amortization_models = [self.Encoder, self.Generator]
        self.amortization_models.extend(self.Hyperprior.amortization_models)

        # Use discriminator if GAN mode enabled and in training/validation
        self.use_discriminator = (
            self.model_type == ModelTypes.COMPRESSION_GAN
            and (self.model_mode != ModelModes.EVALUATION)
        )

        if self.use_discriminator is True:
            assert self.args.discriminator_steps > 0, 'Must specify nonzero training steps for D!'
            self.discriminator_steps = self.args.discriminator_steps
            self.logger.info('GAN mode enabled. Training discriminator for {} steps.'.format(
                self.discriminator_steps))
            self.Discriminator = network.Discriminator(image_dims=self.image_dims,
                context_dims=self.args.latent_dims, C=self.args.latent_channels)
        else:
            self.discriminator_steps = 0
            self.Discriminator = None

        
        self.squared_difference = torch.nn.MSELoss(reduction='none')
        # Expects [-1,1] images or [0,1] with normalize=True flag
        self.perceptual_loss = ps.PerceptualLoss(model='net-lin', net='alex', use_gpu=torch.cuda.is_available())
        
    def store_loss(self, key, loss):
        assert type(loss) == float, 'Call .item() on loss before storage'

        if self.training is True:
            storage = self.storage_train
        else:
            storage = self.storage_test

        if self.writeout is True:
            storage[key].append(loss)

    def compression_forward(self, x):
        """
        Forward pass through encoder, hyperprior, and decoder.

        Inputs
        x:  Input image. Format (N,C,H,W), range [0,1].
            torch.Tensor
        
        Outputs
        intermediates: NamedTuple of intermediate values
        """
        image_dims = tuple(x.size()[1:])  # (C,H,W)

        if self.model_mode == ModelModes.VALIDATION and (self.training is False):
            n_downsamples = self.Encoder.n_downsampling_layers
            factor = 2 ** n_downsamples
            logger.info('Padding to {}'.format(factor))
            x = helpers.pad_factor(x, image_dims, factor)


        # Encoder forward pass
        y = self.Encoder(x)
        hyperinfo = self.Hyperprior(y, spatial_shape=image_dims[2:])

        latents_quantized = hyperinfo.decoded
        total_nbpp = hyperinfo.total_nbpp
        total_qbpp = hyperinfo.total_qbpp

        reconstruction = self.Generator(y)
        # Undo padding
        if self.model_mode == ModelModes.VALIDATION and (self.training is False):
            print('Undoing padding.')
            reconstruction = reconstruction[:, :, :image_dims[1], :image_dims[2]]
        
        intermediates = Intermediates(x, reconstruction, latents_quantized, 
            total_nbpp, total_qbpp)

        return intermediates, hyperinfo

    def discriminator_forward(self, intermediates, generator_train):
        """ Train on gen/real batches simultaneously. """
        x_gen = intermediates.reconstruction
        x_real = intermediates.input_image

        # Alternate between training discriminator and compression models
        if generator_train is False:
            x_gen = x_gen.detach()

        D_in = torch.cat([x_real, x_gen], dim=0)

        latents = intermediates.latents_quantized.detach()
        # latents = torch.cat([latents, latents], dim=0)
        latents = torch.repeat_interleave(latents, 2, dim=0)

        D_out, D_out_logits = self.Discriminator(D_in, latents)
        D_out = torch.squeeze(D_out)
        D_out_logits = torch.squeeze(D_out_logits)

        D_real, D_gen = torch.chunk(D_out, 2, dim=0)
        D_real_logits, D_gen_logits = torch.chunk(D_out_logits, 2, dim=0)

        # Tensorboard
        # real_response, gen_response = D_real.mean(), D_fake.mean()

        return Disc_out(D_real, D_gen, D_real_logits, D_gen_logits)

    def distortion_loss(self, x_gen, x_real):
        # loss in [0,255] space but normalized by 255 to not be too big
        # - Delegate to weighting
        sq_err = self.squared_difference(x_gen*255., x_real*255.) # / 255.
        return torch.mean(sq_err)

    def perceptual_loss_wrapper(self, x_gen, x_real):
        """ Assumes inputs are in [0, 1]. """
        LPIPS_loss = self.perceptual_loss.forward(x_gen, x_real, normalize=True)
        return torch.mean(LPIPS_loss)

    def compression_loss(self, intermediates, hyperinfo):
        
        x_real = intermediates.input_image
        x_gen = intermediates.reconstruction
        # print('X GEN MAX', x_gen.max())
        # print('X GEN MIN', x_gen.min())

        distortion_loss = self.distortion_loss(x_gen, x_real)
        perceptual_loss = self.perceptual_loss_wrapper(x_gen, x_real)

        weighted_distortion = self.args.k_M * distortion_loss
        weighted_perceptual = self.args.k_P * perceptual_loss

        # print('Distortion loss size', weighted_distortion.size())
        # print('Perceptual loss size', weighted_perceptual.size())

        weighted_rate, rate_penalty = losses.weighted_rate_loss(self.args, total_nbpp=intermediates.n_bpp,
            total_qbpp=intermediates.q_bpp, step_counter=self.step_counter)

        # print('Weighted rate loss size', weighted_rate.size())
        weighted_R_D_loss = weighted_rate + weighted_distortion
        weighted_compression_loss = weighted_R_D_loss + weighted_perceptual

        # print('Weighted R-D loss size', weighted_R_D_loss.size())
        # print('Weighted compression loss size', weighted_compression_loss.size())

        # Bookkeeping 
        if (self.step_counter % self.log_interval == 1):
            self.store_loss('rate_penalty', rate_penalty)
            self.store_loss('distortion', distortion_loss.item())
            self.store_loss('perceptual', perceptual_loss.item())
            self.store_loss('n_rate', intermediates.n_bpp.item())
            self.store_loss('q_rate', intermediates.q_bpp.item())
            self.store_loss('n_rate_latent', hyperinfo.latent_nbpp.item())
            self.store_loss('q_rate_latent', hyperinfo.latent_qbpp.item())
            self.store_loss('n_rate_hyperlatent', hyperinfo.hyperlatent_nbpp.item())
            self.store_loss('q_rate_hyperlatent', hyperinfo.hyperlatent_qbpp.item())

            self.store_loss('weighted_rate', weighted_rate.item())
            self.store_loss('weighted_distortion', weighted_distortion.item())
            self.store_loss('weighted_perceptual', weighted_perceptual.item())
            self.store_loss('weighted_R_D', weighted_R_D_loss.item())
            self.store_loss('weighted_compression_loss_sans_G', weighted_compression_loss.item())

        return weighted_compression_loss


    def GAN_loss(self, intermediates, generator_train=False):
        """
        generator_train: Flag to send gradients to generator
        """
        disc_out = self.discriminator_forward(intermediates, generator_train)
        D_loss = losses.gan_loss(disc_out, mode='discriminator_loss')
        G_loss = losses.gan_loss(disc_out, mode='generator_loss')

        # Bookkeeping 
        if (self.step_counter % self.log_interval == 1):
            self.store_loss('D_gen', torch.mean(disc_out.D_gen).item())
            self.store_loss('D_real', torch.mean(disc_out.D_real).item())
            self.store_loss('disc_loss', D_loss.item())
            self.store_loss('gen_loss', G_loss.item())
            self.store_loss('weighted_gen_loss', (self.args.beta * G_loss).item())

        return D_loss, G_loss

    def forward(self, x, generator_train=False, return_intermediates=False, writeout=True):

        self.writeout = writeout

        losses = dict()
        if generator_train is True:
            # Define a 'step' as one cycle of G-D training
            self.step_counter += 1

        intermediates, hyperinfo = self.compression_forward(x)

        if self.model_mode == ModelModes.EVALUATION:
            reconstruction = torch.mul(intermediates.reconstruction, 255.)
            reconstruction = torch.clamp(reconstruction, min=0., max=255.)
            return reconstruction, intermediates.q_bpp

        compression_model_loss = self.compression_loss(intermediates, hyperinfo)

        if self.use_discriminator is True:
            # Only send gradients to generator when training generator via
            # `generator_train` flag
            D_loss, G_loss = self.GAN_loss(intermediates, generator_train)
            weighted_G_loss = self.args.beta * G_loss
            compression_model_loss += weighted_G_loss
            losses['disc'] = D_loss
        
        losses['compression'] = compression_model_loss

        # Bookkeeping 
        if (self.step_counter % self.log_interval == 1):
            self.store_loss('weighted_compression_loss', compression_model_loss.item())

        if return_intermediates is True:
            return losses, intermediates
        else:
            return losses

if __name__ == '__main__':

    logger = helpers.logger_setup(logpath=os.path.join(directories.experiments, 'logs'), filepath=os.path.abspath(__file__))
    device = helpers.get_device()
    logger.info('Using device {}'.format(device))
    storage_train = defaultdict(list)
    storage_test = defaultdict(list)
    model = HificModel(hific_args, logger, storage_train, storage_test, model_type=ModelTypes.COMPRESSION_GAN)
    model.to(device)

    logger.info(model)

    logger.info('ALL PARAMETERS')
    for n, p in model.named_parameters():
        logger.info('{} - {}'.format(n, p.shape))

    logger.info('AMORTIZATION PARAMETERS')
    amortization_named_parameters = itertools.chain.from_iterable(
            [am.named_parameters() for am in model.amortization_models])
    for n, p in amortization_named_parameters:
        logger.info('{} - {}'.format(n, p.shape))

    logger.info('HYPERPRIOR PARAMETERS')
    for n, p in model.Hyperprior.hyperlatent_likelihood.named_parameters():
        logger.info('{} - {}'.format(n, p.shape))

    logger.info('DISCRIMINATOR PARAMETERS')
    for n, p in model.Discriminator.named_parameters():
        logger.info('{} - {}'.format(n, p.shape))

    logger.info("Number of trainable parameters: {}".format(helpers.count_parameters(model)))
    logger.info("Estimated size: {} MB".format(helpers.count_parameters(model) * 4. / 10**6))

    logger.info('Starting forward pass ...')
    start_time = time.time()
    x = torch.randn([10, 3, 256, 256]).to(device)
    losses = model(x)
    compression_loss, disc_loss = losses['compression'], losses['disc']
    print('Compression loss shape', compression_loss.size())
    print('Disc loss shape', disc_loss.size())

    logger.info('Delta t {:.3f}s'.format(time.time() - start_time))


