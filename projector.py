# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
#
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.

"""Project given image to the latent space of pretrained network pickle."""

import copy
import os
from time import perf_counter

import click
import imageio
import numpy as np
import pandas as pd
import PIL.Image
import torch
import torch.nn as nn
import torch.nn.functional as F

import dnnlib
import legacy
from scipy.stats import multivariate_normal


def vgg16_multi_layers_output(model, inputs, layers):
    result = {}
    for name, layer in model.layers.named_modules():
        if name:
            try:
                outputs = layer(inputs)
                inputs = outputs
                if name in layers:
                    result[name] = outputs
            except BaseException:
                break
    return result


def project(
    image2GAN_method,
    G,
    # [C,H,W] and dynamic range [0,255], W & H must match G output resolution
    target: torch.Tensor,
    pose,
    *,
    num_steps=1000,
    w_avg_samples=10000,
    initial_learning_rate=0.1,
    initial_noise_factor=0.05,
    lr_rampdown_length=0.25,
    lr_rampup_length=0.05,
    noise_ramp_length=0.75,
    regularize_noise_weight=1e5,
    verbose=False,
    check_point_w_file=None,
    device: torch.device
):
    assert target.shape == (G.img_channels, G.img_resolution, G.img_resolution)

    convs = ['conv1', 'conv2', 'conv6', 'conv9']

    def logprint(*args):
        if verbose:
            print(*args)

    G = copy.deepcopy(G).eval().requires_grad_(
        False).to(device)  # type: ignore

    # Compute w stats.
    logprint(
        f'Computing W midpoint and stddev using {w_avg_samples} samples...')
    z_samples = np.random.RandomState(123).randn(w_avg_samples, G.z_dim)
    if check_point_w_file:
        w_samples = torch.from_numpy(np.load(check_point_w_file)['w'])
        print(f'You use checkpoint {check_point_w_file}.')
    else:
        w_samples = G.mapping(
            torch.from_numpy(z_samples).to(device),
            None)  # [N, L, C]
    w_samples = w_samples[:, :1, :].cpu().numpy().astype(
        np.float32)       # [N, 1, C]
    w_avg = np.mean(w_samples, axis=0, keepdims=True)      # [1, 1, C]
    w_std = (np.sum((w_samples - w_avg) ** 2) / w_avg_samples) ** 0.5

    # Setup noise inputs.
    noise_bufs = {
        name: buf for (
            name,
            buf) in G.synthesis.named_buffers() if 'noise_const' in name}

    # Load VGG16 feature detector.
    url = 'https://nvlabs-fi-cdn.nvidia.com/stylegan2-ada-pytorch/pretrained/metrics/vgg16.pt'
    with dnnlib.util.open_url(url) as f:
        vgg16 = torch.jit.load(f).eval().to(device)

    # Features for target image.
    target_images = target.unsqueeze(0).to(device).to(torch.float32)
    is_PC = False
    if pose is not None:
        is_PC = True
        pose = pose.unsqueeze(0).to(device).to(torch.float32)
    if target_images.shape[2] > 256:
        target_images = F.interpolate(
            target_images, size=(
                256, 256), mode='area')

    target_convs = vgg16_multi_layers_output(
        vgg16, target_images, convs)

    target_features = vgg16(
        target_images,
        resize_images=False,
        return_lpips=True)

    w_opt = torch.tensor(
        w_avg,
        dtype=torch.float32,
        device=device,
        requires_grad=True)  # pylint: disable=not-callable
    w_out = torch.zeros([num_steps] + list(w_opt.shape[1:]),
                        dtype=torch.float32, device=device)
    optimizer = torch.optim.Adam(
        [w_opt] +
        list(
            noise_bufs.values()),
        betas=(
            0.9,
            0.999),
        lr=initial_learning_rate)

    # Init noise.
    for buf in noise_bufs.values():
        buf[:] = torch.randn_like(buf)
        buf.requires_grad = True

    for step in range(num_steps):
        # Learning rate schedule.
        t = step / num_steps
        w_noise_scale = w_std * initial_noise_factor * \
            max(0.0, 1.0 - t / noise_ramp_length) ** 2
        lr_ramp = min(1.0, (1.0 - t) / lr_rampdown_length)
        lr_ramp = 0.5 - 0.5 * np.cos(lr_ramp * np.pi)
        lr_ramp = lr_ramp * min(1.0, t / lr_rampup_length)
        lr = initial_learning_rate * lr_ramp
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr

        # Synth images from opt_w.
        w_noise = torch.randn_like(w_opt) * w_noise_scale
        ws = (w_opt + w_noise).repeat([1, G.mapping.num_ws, 1])
        if is_PC:
            synth_images = G.synthesis(
                ws, pose, ret_pose=False, noise_mode='const', force_fp32=True)
        else:
            synth_images = G.synthesis(
                ws, noise_mode='const', force_fp32=True)

        # Downsample image to 256x256 if it's larger than that. VGG was built
        # for 224x224 images.
        synth_images = (synth_images + 1) * (255 / 2)
        if synth_images.shape[2] > 256:
            synth_images = F.interpolate(
                synth_images, size=(
                    256, 256), mode='area')

        # Features for synth images.
        synth_features = vgg16(
            synth_images,
            resize_images=False,
            return_lpips=True)

        dist = (target_features - synth_features).square().sum()

        # Noise regularization.
        reg_loss = 0.0
        for v in noise_bufs.values():
            noise = v[None, None, :, :]  # must be [1,1,H,W] for F.avg_pool2d()
            while True:
                reg_loss += (noise *
                             torch.roll(noise, shifts=1, dims=3)).mean()**2
                reg_loss += (noise *
                             torch.roll(noise, shifts=1, dims=2)).mean()**2
                if noise.shape[2] <= 8:
                    break
                noise = F.avg_pool2d(noise, kernel_size=2)

        if image2GAN_method:
            # loss about conv1_1, conv1_2, conv3_2 and conv4_2
            synth_convs = vgg16_multi_layers_output(
                vgg16, target_images, convs)
            for conv in convs:
                dist += (target_convs[conv] - synth_convs[conv]).square().sum()

            # MSE Loss
            mse_loss = nn.MSELoss()
            dist += mse_loss(target_images, synth_images)

        loss = dist + reg_loss * regularize_noise_weight

        # Step
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        optimizer.step()
        logprint(
            f'step {step+1:>4d}/{num_steps}: dist {dist:<4.2f} loss {float(loss):<5.2f}')

        # Save projected W for each optimization step.
        w_out[step] = w_opt.detach()[0]

        # Normalize noise.
        with torch.no_grad():
            for buf in noise_bufs.values():
                buf -= buf.mean()
                buf *= buf.square().mean().rsqrt()

    return w_out.repeat([1, G.mapping.num_ws, 1])

# ----------------------------------------------------------------------------


def get_pose_from_keypoint_string(keypoint, image_size):
    ptlist = keypoint.split(':')
    ptlist = [float(x) for x in ptlist]
    maps = getHeatMap(ptlist, image_size)
    return maps.float()


def get_pose(filename, df, image_size):
    base = os.path.basename(filename)
    keypoint = df[df['name'] == base]['keypoints'].tolist()
    if len(keypoint) > 0:
        keypoint = keypoint[0]
        ptlist = keypoint.split(':')
        ptlist = [float(x) for x in ptlist]
        maps = getHeatMap(ptlist, image_size)
    else:
        maps = torch.zeros(17, 64, 64)
    return maps.float()


def getHeatMap(pose, image_size):
    '''
    pose should be a list of length 51, every 3 number for
    x, y and confidence for each of the 17 keypoints.
    '''

    stack = []
    for i in range(17):
        x = pose[3 * i]

        y = pose[3 * i + 1]
        c = pose[3 * i + 2]

        ratio = 64.0 / image_size
        map = getGaussianHeatMap([x * ratio, y * ratio])

        if c < 0.4:
            map = 0.0 * map
        stack.append(map)

    maps = np.dstack(stack)
    heatmap = torch.from_numpy(maps).transpose(0, -1)
    return heatmap


def getGaussianHeatMap(bonePos):
    width = 64
    x, y = np.mgrid[0:width:1, 0:width:1]
    pos = np.dstack((x, y))

    gau = multivariate_normal(mean=list(bonePos), cov=[
                              [width * 0.02, 0.0], [0.0, width * 0.02]]).pdf(pos)
    return gau


@click.command()
@click.option('--network',
              'network_pkl',
              help='Network pickle filename',
              required=True)
@click.option('--target',
              'target_fname',
              help='Target image file to project to',
              required=True,
              metavar='FILE')
@click.option('--posefile',
              'pose_fname',
              help='pose-file',
              required=True,
              metavar='FILE')
@click.option('--num-steps', help='Number of optimization steps',
              type=int, default=1000, show_default=True)
@click.option('--seed', help='Random seed', type=int,
              default=303, show_default=True)
@click.option('--save-video',
              help='Save an mp4 video of optimization progress',
              type=bool,
              default=True,
              show_default=True)
@click.option('--outdir', help='Where to save the output images',
              required=True, metavar='DIR')
def run_projection(
    image2StyleGAN_method: bool,
    network_pkl: str,
    target_fname: str,
    pose_fname: str,
    outdir: str,
    save_video: bool,
    seed: int,
    num_steps: int,
    pose_list: list
):
    """Project given image to the latent space of pretrained network pickle.
    Examples:
    \b
    python projector.py --outdir=out --target=~/mytargetimg.png \\
        --network=https://nvlabs-fi-cdn.nvidia.com/stylegan2-ada-pytorch/pretrained/ffhq.pkl
    """
    np.random.seed(seed)
    torch.manual_seed(seed)

    # Load networks.
    print('Loading networks from "%s"...' % network_pkl)
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    with dnnlib.util.open_url(network_pkl) as fp:
        G = legacy.load_network_pkl(fp)['G_ema'].requires_grad_(
            False).to(device)  # type: ignore
        if not torch.cuda.is_available():
            G = G.float()

    # Load target image.
    target_pil = PIL.Image.open(target_fname).convert('RGB')
    w, h = target_pil.size
    s = min(w, h)
    target_pil = target_pil.crop(
        ((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2))
    target_pil = target_pil.resize(
        (G.img_resolution, G.img_resolution), PIL.Image.LANCZOS)
    target_uint8 = np.array(target_pil, dtype=np.uint8)

    df = pd.read_csv(pose_fname)
    phase_pose = get_pose(target_fname, df, G.img_resolution)
    # Optimize projection.
    start_time = perf_counter()
    projected_w_steps = project(
        image2StyleGAN_method,
        G,
        target=torch.tensor(target_uint8.transpose(
            [2, 0, 1]), device=device),  # pylint: disable=not-callable
        pose=phase_pose,
        num_steps=num_steps,
        device=device,
        verbose=True
    )
    print(f'Elapsed: {(perf_counter()-start_time):.1f} s')

    # Render debug output: optional video and projected image and W vector.
    os.makedirs(outdir, exist_ok=True)
    if save_video:
        video = imageio.get_writer(
            f'{outdir}/proj.mp4',
            mode='I',
            fps=10,
            codec='libx264',
            bitrate='16M')
        print(f'Saving optimization progress video "{outdir}/proj.mp4"')
        for projected_w in projected_w_steps:
            synth_image = G.synthesis(
                projected_w.unsqueeze(0),
                phase_pose.unsqueeze(0).to(device),
                ret_pose=False,
                noise_mode='const',
                force_fp32=True
            )
            synth_image = (synth_image + 1) * (255 / 2)
            synth_image = synth_image.permute(
                0,
                2,
                3,
                1).clamp(
                0,
                255).to(
                torch.uint8)[0].cpu().numpy()
            video.append_data(np.concatenate(
                [target_uint8, synth_image], axis=1))
        video.close()

    # Save final projected frame and W vector.
    target_pil.save(f'{outdir}/target.png')
    projected_w = projected_w_steps[-1]
    synth_image = G.synthesis(
        projected_w.unsqueeze(0),
        phase_pose.unsqueeze(0).to(device),
        ret_pose=False,
        noise_mode='const',
        force_fp32=True
    )
    synth_image = (synth_image + 1) * (255 / 2)
    synth_image = synth_image.permute(
        0,
        2,
        3,
        1).clamp(
        0,
        255).to(
            torch.uint8)[0].cpu().numpy()
    PIL.Image.fromarray(synth_image, 'RGB').save(f'{outdir}/proj.png')
    np.savez(f'{outdir}/projected_w.npz',
             w=projected_w.unsqueeze(0).cpu().numpy())


def run_projection_from_outside(
        image2StyleGAN_method: bool,
        network_pkl: str,
        target_fname: str,
        outdir: str,
        save_video: bool,
        seed: int,
        num_steps: int,
        keypoint: str,
        output_file_name=None,
        check_point_w_file=None,
):
    """Project given image to the latent space of pretrained network pickle.
    Examples:
    \b
    python projector.py --outdir=out --target=~/mytargetimg.png \\
        --network=https://nvlabs-fi-cdn.nvidia.com/stylegan2-ada-pytorch/pretrained/ffhq.pkl
    """
    np.random.seed(seed)
    torch.manual_seed(seed)

    # Load networks.
    print('Loading networks from "%s"...' % network_pkl)
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    with dnnlib.util.open_url(network_pkl) as fp:
        G = legacy.load_network_pkl(fp)['G_ema'].requires_grad_(
            False).to(device)  # type: ignore
        if not torch.cuda.is_available():
            G = G.float()

    # Load target image.
    target_pil = PIL.Image.open(target_fname).convert('RGB')
    w, h = target_pil.size
    s = min(w, h)
    target_pil = target_pil.crop(
        ((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2))
    target_pil = target_pil.resize(
        (G.img_resolution, G.img_resolution), PIL.Image.LANCZOS)
    target_uint8 = np.array(target_pil, dtype=np.uint8)

    if keypoint:
        phase_pose = get_pose_from_keypoint_string(keypoint, G.img_resolution)
        projected_w_steps = project(
            image2StyleGAN_method,
            G,
            target=torch.tensor(target_uint8.transpose(
                [2, 0, 1]), device=device),  # pylint: disable=not-callable
            pose=phase_pose,
            num_steps=num_steps,
            device=device,
            verbose=True,
            check_point_w_file=check_point_w_file
        )
    else:
        phase_pose = None
        # Optimize projection.
        start_time = perf_counter()
        projected_w_steps = project(
            image2StyleGAN_method,
            G,
            target=torch.tensor(target_uint8.transpose(
                [2, 0, 1]), device=device),  # pylint: disable=not-callable
            pose=phase_pose,
            num_steps=num_steps,
            device=device,
            verbose=True,
            check_point_w_file=check_point_w_file
        )
        print(f'Elapsed: {(perf_counter()-start_time):.1f} s')

    # Render debug output: optional video and projected image and W vector.
    os.makedirs(outdir, exist_ok=True)
    if save_video:
        video = imageio.get_writer(
            f'{outdir}/proj.mp4',
            mode='I',
            fps=10,
            codec='libx264',
            bitrate='16M')
        print(f'Saving optimization progress video "{outdir}/proj.mp4"')
        for projected_w in projected_w_steps:
            if phase_pose is not None:
                synth_image = G.synthesis(
                    projected_w.unsqueeze(0),
                    phase_pose.unsqueeze(0).to(device),
                    ret_pose=False,
                    noise_mode='const',
                    force_fp32=True
                )
            else:
                synth_image = G.synthesis(
                    projected_w.unsqueeze(0),
                    noise_mode='const',
                    force_fp32=True
                )

            synth_image = (synth_image + 1) * (255 / 2)
            synth_image = synth_image.permute(
                0,
                2,
                3,
                1).clamp(
                0,
                255).to(
                torch.uint8)[0].cpu().numpy()
            video.append_data(np.concatenate(
                [target_uint8, synth_image], axis=1))
        video.close()

    # Save final projected frame and W vector.
    target_pil.save(f'{outdir}/target.png')
    projected_w = projected_w_steps[-1]

    if phase_pose is not None:
        synth_image = G.synthesis(
            projected_w.unsqueeze(0),
            phase_pose.unsqueeze(0).to(device),
            ret_pose=False,
            noise_mode='const',
            force_fp32=True
        )
    else:
        synth_image = G.synthesis(
            projected_w.unsqueeze(0),
            noise_mode='const',
            force_fp32=True
        )
    synth_image = (synth_image + 1) * (255 / 2)
    synth_image = synth_image.permute(
        0,
        2,
        3,
        1).clamp(
        0,
        255).to(
        torch.uint8)[0].cpu().numpy()
    if output_file_name:
        PIL.Image.fromarray(synth_image, 'RGB').save(
            f'{outdir}/{output_file_name}.png')
        np.savez(f'{outdir}/{output_file_name}_w.npz',
                 w=projected_w.unsqueeze(0).cpu().numpy())
    else:
        PIL.Image.fromarray(synth_image, 'RGB').save(f'{outdir}/proj.png')
        np.savez(f'{outdir}/projected_w.npz',
                 w=projected_w.unsqueeze(0).cpu().numpy())
    np.savez(f'{outdir}/{output_file_name}_w.npz',
             w=projected_w.unsqueeze(0).cpu().numpy())
    return projected_w.unsqueeze(0).cpu().numpy()
# ----------------------------------------------------------------------------


if __name__ == "__main__":
    run_projection()  # pylint: disable=no-value-for-parameter

# ----------------------------------------------------------------------------
