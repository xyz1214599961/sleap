"""Transformers for finding instance centroids."""

import tensorflow as tf
import attr
from typing import Optional, List, Text
import sleap
from sleap.nn.data.utils import ensure_list


def find_points_bbox_midpoint(points: tf.Tensor) -> tf.Tensor:
    """Find the midpoint of the bounding box of a set of points.
    
    Args:
        instances: A tf.Tensor of dtype tf.float32 and of shape (..., n_points, 2),
            i.e., rank >= 2.
    
    Returns:
        The midpoints between the bounds of each set of points. The output will be of
        shape (..., 2), reducing the rank of the input by 1. NaNs will be ignored in the
        calculation.
        
    Notes:
        The midpoint is calculated as:
            xy_mid = xy_min + ((xy_max - xy_min) / 2)
                   = ((2 * xy_min) / 2) + ((xy_max - xy_min) / 2)
                   = (2 * xy_min + xy_max - xy_min) / 2
                   = (xy_min + xy_max) / 2
    """
    pts_min = tf.reduce_min(points, axis=-2)
    pts_max = tf.reduce_max(points, axis=-2)
    return (pts_max + pts_min) * 0.5


def get_instance_anchors(instances: tf.Tensor, anchor_inds: tf.Tensor) -> tf.Tensor:
    """Gather the anchor points of a set of instances.
    
    Args:
        instances: A tensor of shape (n_instances, n_nodes, 2) containing instance
            points. This must be rank-3 even if a single instance is present.
        anchor_inds: A tensor of shape (n_instances,) and dtype tf.int32. These specify
            the index of the anchor node for each instance.
    
    Returns:
        A tensor of shape (n_instances, 2) containing the anchor points for each
        each instance. This is basically a slice along the nodes axis, where each
        instance may potentially have a different node to use as an anchor.
    """
    inds = tf.stack([tf.range(tf.shape(anchor_inds)[0]), anchor_inds], axis=-1)
    return tf.gather_nd(instances, inds)


@attr.s(auto_attribs=True)
class InstanceCentroidFinder:
    """Data transformer to add centroid information to instances.

    This is useful as a transformation to data streams that will be used in centroid
    networks or for instance cropping.

    Attributes:
        center_on_anchor_part: If True, specifies that centering should be done relative
            to a body part rather than the midpoint of the instance bounding box. If
            False, the midpoint of the bounding box of all points will be used.
        anchor_part_names: List of strings specifying the body part name in each
            skeleton to use as anchors for centering. If `center_on_anchor_part` is
            False, this has no effect and does not need to be specified.
        skeletons: List of `sleap.Skeleton`s to use for looking up the index of the
            anchor body parts. If `center_on_anchor_part` is False, this has no effect
            and does not need to be specified.
    """

    center_on_anchor_part: bool = False
    anchor_part_names: Optional[List[Text]] = attr.ib(default=None,
        converter=attr.converters.optional(ensure_list)
    )
    skeletons: Optional[List[sleap.Skeleton]] = attr.ib(default=None,
        converter=attr.converters.optional(ensure_list)
    )

    @property
    def input_keys(self) -> List[Text]:
        """Return the keys that incoming elements are expected to have."""
        if self.center_on_anchor_part:
            return ["instances", "skeleton_inds"]
        else:
            return ["instances"]

    @property
    def output_keys(self) -> List[Text]:
        """Return the keys that outgoing elements will have."""
        return self.input_keys + ["centroids"]

    def transform_dataset(self, ds_input: tf.data.Dataset) -> tf.data.Dataset:
        """Create a dataset that contains centroids computed from the inputs.

        Args:
            ds_input: A dataset with "instances" key containing instance points in a
                tf.float32 tensor of shape (n_instances, n_nodes, 2). If centering on
                anchor parts, a "skeleton_inds" key of dtype tf.int32 and shape
                (n_instances,) must also be present to indicate which skeleton is
                associated with each instance. These must match the order in the
                `skeletons` attribute of this class.

        Returns:
            A `tf.data.Dataset` with elements containing a "centroids" key containing
            a tf.float32 tensor of shape (n_instances, 2) with the computed centroids.
        """
        if self.center_on_anchor_part:
            # Create lookup table for converting part names to indices.
            part_inds = tf.convert_to_tensor(
                [
                    skeleton.node_names.index(part_name)
                    for skeleton, part_name in zip(
                        self.skeletons, self.anchor_part_names
                    )
                ],
                tf.int32,
            )

            def find_centroids(frame_data):
                """Local processing function for dataset mapping."""
                # Find the anchor points.
                anchor_inds = tf.gather(part_inds, frame_data["skeleton_inds"])
                anchors = get_instance_anchors(frame_data["instances"], anchor_inds)

                # Find the bounding box midpoints.
                mid_pts = find_points_bbox_midpoint(frame_data["instances"])

                # Keep the midpoints of the bounding boxes where anchors are missing.
                centroids = tf.where(tf.math.is_nan(anchors), mid_pts, anchors)

                # Update and return.
                frame_data["centroids"] = centroids
                return frame_data

        else:

            def find_centroids(frame_data):
                """Local processing function for dataset mapping."""
                # Find the bounding box midpoints.
                mid_pts = find_points_bbox_midpoint(frame_data["instances"])

                # Update and return.
                frame_data["centroids"] = mid_pts
                return frame_data

        # Map transformation.
        ds_output = ds_input.map(
            find_centroids, num_parallel_calls=tf.data.experimental.AUTOTUNE
        )
        return ds_output