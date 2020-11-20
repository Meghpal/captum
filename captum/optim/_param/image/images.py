import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

try:
    from PIL import Image
except (ImportError, AssertionError):
    print("The Pillow/PIL library is required to use Captum's Optim library")

from captum.optim._param.image.transform import ToRGB


class ImageTensor(torch.Tensor):
    def __init__(self, data, **kwargs):
        if not isinstance(data, torch.Tensor):
            data = torch.as_tensor(data, **kwargs)
        self._t = data

    @classmethod
    def open(cls, path):
        img_np = Image.open(path).convert("RGB")
        img_np = np.array(img_np).astype(np.float32)
        return cls(img_np.transpose(2, 0, 1) / 255)

    @classmethod
    def __torch_function__(self, func, types, args=(), kwargs=None):
        if kwargs is None:
            kwargs = {}
        args = [a._t if hasattr(a, "_t") else a for a in args]
        return super().__torch_function__(func, types, args, **kwargs)

    def __repr__(self):
        return f"ImageTensor(value={self._t})"

    def show(self, scale=255):
        if len(self.shape) == 3:
            numpy_thing = self.cpu().detach().numpy().transpose(1, 2, 0) * scale
        elif len(self.shape) == 4:
            numpy_thing = self.cpu().detach().numpy()[0].transpose(1, 2, 0) * scale
        plt.imshow(numpy_thing.astype(np.uint8))
        plt.axis("off")
        plt.show()

    def export(self, filename, scale=255):
        if len(self.shape) == 3:
            numpy_thing = self.cpu().detach().numpy().transpose(1, 2, 0) * scale
        elif len(self.shape) == 4:
            numpy_thing = self.cpu().detach().numpy()[0].transpose(1, 2, 0) * scale
        im = Image.fromarray(numpy_thing.astype("uint8"), "RGB")
        im.save(filename)

    def cpu(self):
        return self

    def cuda(self):
        return CudaImageTensor(self._t, device="cuda")


class CudaImageTensor(object):
    def __init__(self, data, **kwargs):
        self._t = torch.as_tensor(data, **kwargs)

    @classmethod
    def __torch_function__(self, func, types, args=(), kwargs=None):
        if kwargs is None:
            kwargs = {}
        args = [a._t if hasattr(a, "_t") else a for a in args]
        return super().__torch_function__(func, types, args, **kwargs)

    def __repr__(self):
        return f"CudaImageTensor(value={self._t})"

    @property
    def shape(self):
        return self._t.shape

    def show(self):
        self.cpu().show()

    def export(self, filename):
        self.cpu().export(filename)

    def cpu(self):
        return ImageTensor(self._t.cpu())

    def cuda(self):
        return self


# mean = [0.485, 0.456, 0.406]
# std = [0.229, 0.224, 0.225]

# normalize = transforms.Normalize(mean=mean, std=std)

# mean = torch.Tensor(mean)[None, :, None, None]
# std = torch.Tensor(std)[None, :, None, None]


# def denormalize(x: torch.Tensor):
#     return std * x + mean


def logit(p: torch.Tensor, epsilon=1e-6) -> torch.Tensor:
    p = torch.clamp(p, min=epsilon, max=1.0 - epsilon)
    assert p.min() >= 0 and p.max() < 1
    return torch.log(p / (1 - p))


# def jitter(x: torch.Tensor, pad_width=2, pad_value=0.5):
#     _, _, H, W = x.shape
#     y = F.pad(x, 4 * (pad_width,), value=pad_value)
#     idx, idy = np.random.randint(low=0, high=2 * pad_width, size=(2,))
#     return y[:, :, idx : idx + H, idy : idy + W]


# def color_correction():
#     S = np.asarray(
#         [[0.26, 0.09, 0.02], [0.27, 0.00, -0.05], [0.27, -0.09, 0.03]]
#     ).astype("float32")
#     C = S / np.max(np.linalg.norm(S, axis=0))
#     C = torch.Tensor(C)
#     return C.transpose(0, 1)


# def upsample():
#     upsample = torch.nn.Upsample(scale_factor=1.1, mode="bilinear",
#        align_corners=True)

#     def up(x):
#         upsample.scale_factor = (
#             1 + np.random.randn(1)[0] / 50,
#             1 + np.random.randn(1)[0] / 50,
#         )
#         return upsample(x)

#     return up


class InputParameterization(torch.nn.Module):
    def forward(self):
        raise NotImplementedError


class ImageParameterization(InputParameterization):
    def setup_batch(
        self, x: torch.Tensor, batch: int = 1, dim: int = 3
    ) -> torch.Tensor:
        assert batch > 0
        x = x.unsqueeze(0) if x.dim() == dim and batch == 1 else x
        x = (
            torch.stack([x.clone() for b in range(batch)])
            if x.dim() == dim and batch > 1
            else x
        )
        return x

    def set_image(self, x: torch.Tensor):
        ...


class FFTImage(ImageParameterization):
    """Parameterize an image using inverse real 2D FFT"""

    def __init__(
        self, size, channels: int = 3, batch: int = 1, init: torch.Tensor = None
    ):
        super().__init__()
        if init is None:
            assert len(size) == 2
            self.size = size
        else:
            assert init.shape[0] == 3
            self.size = (init.size(1), init.size(2))

        frequencies = FFTImage.rfft2d_freqs(*self.size)
        scale = 1.0 / torch.max(
            frequencies,
            torch.full_like(frequencies, 1.0 / (max(self.size[0], self.size[1]))),
        )
        scale = scale * ((self.size[0] * self.size[1]) ** (1 / 2))
        spectrum_scale = scale[None, :, :, None]
        self.register_buffer("spectrum_scale", spectrum_scale)

        if init is None:
            coeffs_shape = (channels, self.size[0], self.size[1] // 2 + 1, 2)
            random_coeffs = torch.randn(
                coeffs_shape
            )  # names=["C", "H_f", "W_f", "complex"]
            self.fourier_coeffs = random_coeffs / 50
        else:
            self.fourier_coeffs = torch.rfft(init, signal_ndim=2) / spectrum_scale

        self.fourier_coeffs = self.setup_batch(self.fourier_coeffs, batch, 4)
        self.fourier_coeffs = nn.Parameter(self.fourier_coeffs)

    @staticmethod
    def rfft2d_freqs(height: int, width: int) -> torch.Tensor:
        """Computes 2D spectrum frequencies."""
        fy = FFTImage.pytorch_fftfreq(height)[:, None]
        # on odd input dimensions we need to keep one additional frequency
        wadd = 2 if width % 2 == 1 else 1
        fx = FFTImage.pytorch_fftfreq(width)[: width // 2 + wadd]
        return torch.sqrt((fx * fx) + (fy * fy))

    @staticmethod
    def pytorch_fftfreq(v: int, d: float = 1.0) -> torch.Tensor:
        """PyTorch version of np.fft.fftfreq"""
        results = torch.empty(v)
        s = (v - 1) // 2 + 1
        results[:s] = torch.arange(0, s)
        results[s:] = torch.arange(-(v // 2), 0)
        return results * (1.0 / (v * d))

    def set_image(self, correlated_image: torch.Tensor):
        coeffs = torch.rfft(correlated_image, signal_ndim=2)
        self.fourier_coeffs = coeffs / self.spectrum_scale

    def forward(self):
        h, w = self.size
        scaled_spectrum = self.fourier_coeffs * self.spectrum_scale
        output = torch.irfft(scaled_spectrum, signal_ndim=2)[:, :, :h, :w]
        return output.refine_names("B", "C", "H", "W")


class PixelImage(ImageParameterization):
    def __init__(
        self, size=None, channels: int = 3, batch: int = 1, init: torch.Tensor = None
    ):
        super().__init__()
        if init is None:
            assert size is not None and channels is not None and batch is not None
            init = torch.randn([channels, size[0], size[1]]) / 10 + 0.5
        else:
            assert init.shape[0] == 3
        init = self.setup_batch(init, batch)
        self.image = nn.Parameter(init)

    def forward(self):
        return self.image.refine_names("B", "C", "H", "W")

    def set_image(self, correlated_image: torch.Tensor):
        self.image = nn.Parameter(correlated_image)


class LaplacianImage(ImageParameterization):
    def __init__(
        self, size=None, channels: int = 3, batch: int = 1, init: torch.Tensor = None
    ):
        super().__init__()
        power = 0.1
        self.tensor_params = []
        self.scaler = []
        for scale in [1, 2, 4, 8, 16, 32]:
            h, w = int(size[0] // scale), int(size[1] // scale)
            if init is None:
                x = torch.randn([1, channels, h, w]) / 10
            else:
                x = F.interpolate(
                    init.clone().unsqueeze(0), size=(h, w), mode="bilinear"
                )
                x = x / 6  # Prevents output from being all white
            upsample = torch.nn.Upsample(scale_factor=scale, mode="nearest")
            x = x * (scale ** power) / (32 ** power)
            x = torch.nn.Parameter(x)
            self.tensor_params.append(x)
            self.scaler.append(upsample)
        self.tensor_params = torch.nn.ParameterList(self.tensor_params)

    def forward(self):
        A = []
        for xi, upsamplei in zip(self.tensor_params, self.scaler):
            A.append(upsamplei(xi))
        return (
            (torch.sum(torch.cat(A), 0) + 0.5)
            .unsqueeze(0)
            .refine_names("B", "C", "H", "W")
        )


class NaturalImage(ImageParameterization):
    r"""Outputs an optimizable input image.

    By convention, single images are CHW and float32s in [0,1].
    The underlying parameterization is decorrelated via a ToRGB transform.
    When used with the (default) FFT parameterization, this results in a fully
    uncorrelated image parameterization. :-)

    If a model requires a normalization step, such as normalizing imagenet RGB values,
    or rescaling to [0,255], it has to perform that step inside its computation.
    For example, our GoogleNet factory function has a `transform_input=True` argument.
    """

    def __init__(
        self,
        size=None,
        channels: int = 3,
        batch: int = 1,
        Parameterization=FFTImage,
        init: torch.Tensor = None,
    ):
        super().__init__()

        self.decorrelate = ToRGB(transform_name="klt")
        if init is not None:
            init = self.decorrelate.decorrelate_init(init)
            self.squash_func = lambda x: x.clamp(0, 1)
        else:
            self.squash_func = lambda x: torch.sigmoid(x)
        self.parameterization = Parameterization(
            size=size, channels=channels, batch=batch, init=init
        )

    def forward(self):
        image = self.parameterization()
        image = self.decorrelate(image)
        image = image.rename(None)  # TODO: the world is not yet ready
        return CudaImageTensor(self.squash_func(image))

    def set_image(self, image):
        logits = logit(image, epsilon=1e-4)
        correlated = self.decorrelate(logits, inverse=True)
        self.parameterization.set_image(correlated)
