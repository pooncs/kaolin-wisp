# Copyright (c) 2022, NVIDIA CORPORATION & AFFILIATES.  All rights reserved.
#
# NVIDIA CORPORATION & AFFILIATES and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION & AFFILIATES is strictly prohibited.

import numpy as np
import imgui
from .widget_imgui import WidgetImgui
from wisp.framework.state import WispState
from wisp.models.grids import OctreeGrid
from .widget_property_editor import WidgetPropertyEditor


class WidgetOctreeGrid(WidgetImgui):
    def __init__(self):
        super().__init__()
        self.properties_widget = WidgetPropertyEditor()

    def paint(self, state: WispState, octree: OctreeGrid = None, *args, **kwargs):
        if octree is not None:
            properties = {
                "Feature Dims": octree.feature_dim,
                "Total LODs": octree.max_lod,
                "Active feature LODs": ', '.join([str(x) for x in octree.active_lods]),
                "Interpolation": octree.interpolation_type,
                "Multiscale aggregation": octree.multiscale_type
            }
            self.properties_widget.paint(state=state, properties=properties)

            pyramid = octree.blas.pyramid
            if pyramid is not None and pyramid.shape[1] > 1:
                points_per_lod = pyramid[0, :-2].cpu().numpy()
                imgui.text(f"Occupancy per LOD (%):")
                occupancy_hist = [occupied_cells / 8**lod for lod, occupied_cells in enumerate(points_per_lod)]
                width, height = imgui.get_content_region_available()
                imgui.plot_histogram(label="##octree_grid", values=np.array(occupancy_hist, dtype=np.float32),
                                     graph_size=(width, 20))
