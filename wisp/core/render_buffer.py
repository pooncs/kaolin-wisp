# Copyright (c) 2022, NVIDIA CORPORATION & AFFILIATES.  All rights reserved.
#
# NVIDIA CORPORATION & AFFILIATES and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION & AFFILIATES is strictly prohibited.

from __future__ import annotations
from dataclasses import fields, dataclass, make_dataclass
from typing import Optional, List, Tuple, Set, Dict, Union
from functools import partial
import numpy as np
import torch
from wisp.core.channels import Channel, create_default_channel

__RB_VARIANTS__ = dict()

# TODO: per channel we need: blend(), normalize() (to [0, 1])  - discretize can use normalize()
# TODO: rgba, a, rgb, bgr methods

@dataclass
class RenderBuffer:
    rgb    : Optional[torch.Tensor] = None     # rgb is usually the shaded RGB color.
    alpha  : Optional[torch.Tensor] = None     # alpha is usually the alpha component of RGB-A.
    depth  : Optional[torch.Tensor] = None     # depth is usually the distance to the surface hit point.

    # Renderbuffer supports additional custom channels passed to the Renderbuffer constructor.
    # Access to these channels is identical to the default fields of the RenderBuffer class.
    # Some example of custom channels used throughout wisp:
    #     xyz=None,         # xyz is usually the xyz position for the surface hit point.
    #     hit=None,         # hit is usually a segmentation mask of hit points.
    #     normal=None,      # normal is usually the surface normal at the hit point.
    #     shadow =None,     # shadow is usually some additional buffer for shadowing.
    #     ao=None,          # ao is usually some addition buffer for ambient occlusion.
    #     ray_o=None,       # ray_o is usually the ray origin.
    #     ray_d=None,       # ray_d is usually the ray direction.
    #     err=None,         # err is usually some error metric against the ground truth.
    #     gts=None,         # gts is usually the ground truth image.

    def __new__(cls, *args, **kwargs):
        """ If additional channels were specified, create a specialized Renderbuffer class containing them as dataclass
            fields.
        """
        # kwargs contains all channels given to the constructor,
        # filter to keep only new channels which aren't listed as default fields under the Renderbuffer class
        class_fields = [f.name for f in fields(RenderBuffer)]
        new_fields = [k for k in kwargs.keys() if k not in class_fields]
        if len(new_fields) > 0:
            # If there are new custom channels, see if a specialized class for this combination of channels
            # have been created before. If so, reuse it.
            class_key = frozenset(new_fields)
            rb_class = __RB_VARIANTS__.get(class_key)
            if rb_class is None:
                # First time this combination of channels is encountered:
                # create a Renderbuffer dataclass variant with new additional channels as fields
                rb_class = make_dataclass(f'Renderbuffer_{len(__RB_VARIANTS__)}',
                                          fields=[(k, Optional[torch.Tensor], None) for k in kwargs.keys()],
                                          bases=(RenderBuffer,))
                __RB_VARIANTS__[class_key] = rb_class   # Cache for future __new__ calls
            return super(RenderBuffer, rb_class).__new__(rb_class)  # Construct the new Renderbuffer variant
        else:
            return super(RenderBuffer, cls).__new__(cls)    # No new fields, just build the default Renderbuffer

    def __iter__(self):
        """ Creates an iterator on the Renderbuffer fields as {name: tensor}. """
        # A tensor safe version:
        # the dataclasse asdict function performs a deepcopy which does not respect tensors with gradients.
        return iter((f.name, getattr(self, f.name)) for f in fields(self))

    def __getattr__(self, item):
        """ Invoked when an attribute is not found: used to return a default None for unknown channels. """
        if item.startswith('__') and item.endswith('__'):
            raise AttributeError
        else:
            return None  # Renderbuffer silently returns None when unknown channels are accessed

    @property
    def rgba(self) -> Optional[torch.Tensor]:
        if self.alpha is None or self.rgb is None:
            return None
        else:
            return torch.cat((self.rgb, self.alpha), dim=-1)

    @rgba.setter
    def rgba(self, val: Optional[torch.Tensor]) -> None:
        self.rgb = val[..., 0:-1]
        self.alpha = val[..., -1:]

    @property
    def channels(self) -> Set[str]:
        return set([f.name for f in fields(self)])

    def has_channel(self, name: str) -> bool:
        return name in self.channels

    def get_channel(self, name: str) -> Optional[torch.Tensor]:
        return getattr(self, name)

    @staticmethod
    def _join_fields(rb1, rb2):
        """ Creates a joint mapping of renderbuffer fields in a format of
            {
                channel1_name: (rb1.c1, rb2.c1),
                channel2_name: (rb1.c2, rb2.cb),
                channel3_name: (rb1.c1, None),  # rb2 doesn't define channel3
            }
            If a renderbuffer does not have define a specific channel, None is returned.
        """
        joint_fields = rb1.channels.union(rb2.channels)
        return {f: (getattr(rb1, f, None), getattr(rb2, f, None)) for f in joint_fields}

    def _apply(self, fn) -> RenderBuffer:
        """ Applies the function fn on each of the Renderbuffer channels, if not None.
            Returns a new instance with the processed channels.
        """
        data = {}
        for f in fields(self):
            attr = getattr(self, f.name)
            data[f.name] = None if attr is None else fn(attr)
        return RenderBuffer(**data)

    @staticmethod
    def _apply_on_pair(rb1, rb2, fn) -> RenderBuffer:
        """ Applies the function fn on each of the Renderbuffer channels, if not None.
            Returns a new instance with the processed channels.
        """
        joint_fields = RenderBuffer._join_fields(rb1, rb2)  # Union of field names and tuples of values
        combined_channels = map(fn, joint_fields.values())  # Invoke on pair per Renderbuffer field
        return RenderBuffer(**dict(zip(joint_fields.keys(), combined_channels)))    # Pack combined fields to a new rb

    def __add__(self, other) -> RenderBuffer:
        """ Renderbuffer is a sparse object of pixels.
            By default, __add__ performs concatenation of values per channel.
        """
        return self.cat(other)

    def cat(self, other) -> RenderBuffer:
        def _cat(pair):
            if None not in pair:
                # Concatenating tensors of different dims where one is unsqueezed with dimensionality 1
                if pair[0].ndim == (pair[1].ndim + 1) and pair[0].shape[-1] == 1:
                    pair = (pair[0], pair[1].unsqueeze(-1))
                elif pair[1].ndim == (pair[0].ndim + 1) and pair[1].shape[-1] == 1:
                    pair = (pair[0].unsqueeze(-1), pair[1])
                return torch.cat(pair)
            elif pair[0] is not None and pair[1] is None:   # Channel is None for other but not self
                return pair[0]
            elif pair[0] is None and pair[1] is not None:   # Channel is None for self but not other
                return pair[1]
            else:
                return None

        return RenderBuffer._apply_on_pair(self, other, _cat)

    def blend(self, other: RenderBuffer, channel_kit: Dict[str, Channel]) -> RenderBuffer:
        assert self.depth is not None and other.depth is not None, "Cannot blend renderbuffers without depth values."
        # TODO (operel): In the future depth front / back may depend on the choice of NDC space
        #   (currently objects in the front --> lower depth)
        mask: torch.Tensor = self.depth <= other.depth
        blended = dict()
        joint_fields = RenderBuffer._join_fields(self, other)  # Union of field names and tuples of values
        alpha_pair = (self.alpha, other.alpha)
        is_alpha_blending = None not in alpha_pair
        for field in joint_fields:
            pair = getattr(self, field), getattr(other, field)

            if None not in pair:    # Actual blending occurs when both RBs have information about this channel
                # Alpha channel available, invoke blending function
                if is_alpha_blending:
                    # Sort c1, c2 by depth:
                    # c1 is the channel from the RB with lower depth
                    # alpha1 is the corresponding alpha channel used for blending
                    c1 = torch.where(mask, pair[0], pair[1])
                    alpha1 = torch.where(mask, alpha_pair[0], alpha_pair[1])
                    c2 = torch.where(mask, pair[1], pair[0])
                    alpha2 = torch.where(mask, alpha_pair[1], alpha_pair[0])

                    # The channel kit stores information about how the channel should be blended.
                    # If no information have been registered for this channel type, resort to default settings.
                    channel_info = channel_kit.get(field, create_default_channel())
                    blend = channel_info.blend_fn
                    out = blend(c1, c2, alpha1, alpha2)
                else:   # Alpha channel is n/a, resort to painters algorithm and choose front facing pixel
                    out = torch.where(mask, pair[0], pair[1])
            elif pair[0] is not None and pair[1] is None:
                out = pair[0]   # Channel is n/a in other
            elif pair[0] is None and pair[1] is not None:
                out = pair[1]   # Channel is n/a in self
            else:
                out = None      # Channel is n/a in both self and other
            blended[field] = out
        return RenderBuffer(**blended)

    def transpose(self) -> RenderBuffer:
        """ Permutes dimensions 0 and 1 of each channel.
            The rest of the channel dimensions will remain in the same order.
        """
        fn = lambda x : x.permute(1, 0, *tuple(range(2, x.ndim)))
        return self._apply(fn)

    def scale(self, size: Tuple, interpolation='bilinear') -> RenderBuffer:
        """ Warning: for non-floating point channels, this function will upcast to floating point dtype
            to perform interpolation, and will then re-cast back to the original dtype.
            Hence truncations due to rounding may occur.
        """
        def _scale(x):
            # Some versions of torch don't support direct interpolation of non-fp tensors
            dtype = x.dtype
            if not torch.is_floating_point(x):
                x = x.float()
            # TODO (operel): Extend to arbitrary dimensions and test
            x = x.permute(2, 0, 1)[None]
            x = torch.nn.functional.interpolate(x, size=size, mode=interpolation)
            x = x[0].permute(1, 2, 0)
            if x.dtype != dtype:
                x = torch.round(x).to(dtype)
            return x
        return self._apply(_scale)

    def numpy_dict(self) -> Dict[str, torch.Tensor]:
        """This function will return a dictionary for EXR.

        This function will return a dictionary suitable for use with `pyexr` to output multi-channel
        EXR images which can be viewed interactively with software like `tev`.

        This is suitable for debugging geometric quantities like ray origins and ray directions.
        """
        _dict = dict(iter(self))
        _dict = {k:v.numpy() for k,v in _dict.items() if v is not None}
        return _dict

    def exr_dict(self) -> Dict[str, torch.Tensor]:
        """This function will return a dictionary for EXR.

        This function will return a dictionary suitable for use with `pyexr` to output multi-channel
        EXR images which can be viewed interactively with software like `tev`.

        This is suitable for debugging geometric quantities like ray origins and ray directions.
        """
        _dict = self.numpy_dict()
        if 'rgb' in _dict:
            _dict['default'] = _dict['rgb']
            del _dict['rgb']
        return _dict

    def image(self) -> RenderBuffer:
        """This function will return a copy of the RenderBuffer which will contain 8-bit [0,255] images.

        This function is used to output a RenderBuffer suitable for saving as a 8-bit RGB image (e.g. with
        Pillow). Since this quantization operation permanently loses information, this is not an inplace
        operation and will return a copy of the RenderBuffer. Currently this function will only return
        the hit segmentation mask, normalized depth, RGB, and the surface normals.

        If users want custom behaviour, users can implement your own conversion function which takes a
        RenderBuffer as input.
        """
        norm = lambda arr : ((arr + 1.0) / 2.0) if arr is not None else None
        bwrgb = lambda arr : torch.cat([arr]*3, dim=-1) if arr is not None else None
        rgb8 = lambda arr : (arr * 255.0) if arr is not None else None

        channels = dict()
        if self.rgb is not None:
            channels['rgb'] = rgb8(self.rgb)
        if self.alpha is not None:
            channels['alpha'] = rgb8(self.alpha)
        if self.depth is not None:
            # If the relative depth is respect to some camera clipping plane, the depth should
            # be clipped in advance.
            relative_depth = self.depth / (torch.max(self.depth) + 1e-8)
            channels['depth'] = rgb8(bwrgb(relative_depth))

        # TODO (operel): Write rgba channel

        # TODO (operel): Handle channels in a more general way
        if hasattr(self, 'hit') and self.hit is not None:
            channels['hit'] = rgb8(bwrgb(self.hit))
        else:
            channels['hit'] = None
        if hasattr(self, 'normal') and self.normal is not None:
            channels['normal'] = rgb8(norm(self.normal))
        else:
            channels['normal'] = None

        return RenderBuffer(**channels)

    @staticmethod
    def mean(*rblst) -> RenderBuffer:
        """This function will take a list of RenderBuffers and return the mean of the RenderBuffers.
        None channels count as zero towards the average (unless all channels are None, in which case the mean will be
        None).
        This is useful to implement things like antialiasing through aggregating multiple samples per ray.
        """
        def _sum(pair):
            if None not in pair:
                return pair[0] + pair[1]
            elif pair[0] is not None and pair[1] is None:   # Channel is None for other but not self
                return pair[0]
            elif pair[0] is None and pair[1] is not None:   # Channel is None for self but not other
                return pair[1]
            else:
                return None

        rb = RenderBuffer()
        n = len(rblst)
        for item in rblst:
            rb = RenderBuffer._apply_on_pair(rb, item, _sum)
        div = partial(torch.div, other=float(n))
        return rb._apply(div)

    def reshape(self, *dims : List[int]) -> RenderBuffer:
        fn = lambda x : x.reshape(*dims)
        return self._apply(fn)

    def to(self, *args, **kwargs) -> RenderBuffer:
        fn = lambda x : x.to(*args, **kwargs)
        return self._apply(fn)

    def cuda(self) -> RenderBuffer:
        fn = lambda x : x.cuda()
        return self._apply(fn)

    def cpu(self) -> RenderBuffer:
        fn = lambda x : x.cpu()
        return self._apply(fn)

    def detach(self) -> RenderBuffer:
        fn = lambda x : x.detach()
        return self._apply(fn)

    def byte(self) -> RenderBuffer:
        fn = lambda x : x.byte()
        return self._apply(fn)

    def half(self) -> RenderBuffer:
        fn = lambda x : x.half()
        return self._apply(fn)

    def float(self) -> RenderBuffer:
        fn = lambda x : x.float()
        return self._apply(fn)

    def double(self) -> RenderBuffer:
        fn = lambda x : x.double()
        return self._apply(fn)
