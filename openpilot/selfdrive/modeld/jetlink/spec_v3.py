"""Cinque v3's ONNX contract, read through the property names the rest of jetlink uses.

The v3 export keeps its recurrent state on the serving host: its inputs are one
warped frame pair (new_img), the three live scalars (desire, traffic_convention,
action_t) and three state queues it feeds back to itself, and its outputs are the
usual model output plus those queues' next values. Protocol v3 already leaves the
queues to the host (see compat.py), and the wire carries exactly the scalars the
pinned v2 packed layout starts with - the same 48 bytes.

So only the *names* differ. Every jetlink module reads a contract through
ModelSpec's v2-shaped properties (warped_shape, packed_nelem, feat_dim, ...).
Redefining them here keeps the SPEC this branch publishes byte-identical to the
one the host derives from the same ONNX - which is what mac.validate_spec
compares - while daemon.py, link.py, model.py and compat.py stay unchanged.
"""
from __future__ import annotations

from jetlink.spec import ModelSpec

IMAGE = 'new_img'
STATE_FEAT = 'state_feat_q'
STATE_DESIRE = 'state_desire_q'


def is_v3(raw: dict) -> bool:
  """True for a Cinque v3 export: its image input is the warped pair already."""
  return IMAGE in (raw.get('input_shapes') or {})


def load_spec(raw: dict) -> ModelSpec:
  """The spec class that knows how to answer for this contract."""
  return (ModelSpecV3 if is_v3(raw) else ModelSpec).from_dict(raw)


class ModelSpecV3(ModelSpec):
  """A v3 ONNX spec behind the v2 property names jetlink already reads."""

  @property
  def img_shape(self) -> tuple[int, ...]:
    # No separate img/big_img in v3: the wire image is the warped pair, so this
    # answers with it rather than raising for a name the model does not have.
    return self.warped_shape

  @property
  def n_frames(self) -> int:
    # new_img stacks one narrow and one wide frame, as openpilot's warp emits them.
    return 2

  @property
  def model_hw(self) -> tuple[int, int]:
    shape = self.input_shapes[IMAGE]
    return shape[2], shape[3]

  @property
  def warped_shape(self) -> tuple[int, int, int, int]:
    return self.input_shapes[IMAGE]  # already narrow+wide stacked: (2, 6, H, W)

  @property
  def feat_dim(self) -> int:
    # state_feat_q's last axis is the flattened per-frame feature, exactly as
    # v2's features_buffer[2:] is.
    return self.input_shapes[STATE_FEAT][-1]

  @property
  def packed_shapes(self) -> dict[str, tuple[int, ...]]:
    """Same order and sizes as the pinned v2 layout.

    The first three are the scalars a v3 host is sent - the 48 bytes compat.py
    takes from the front of this buffer - and prev_feat stays a local slot that
    modeld fills from hidden_state and the wire never carries.
    """
    return {
      'desire': (self.input_shapes['desire'][-1],),
      'traffic_convention': tuple(self.input_shapes['traffic_convention']),
      'action_t': tuple(self.input_shapes['action_t']),
      'prev_feat': (1, self.feat_dim),
    }

  @property
  def feat_q_shape(self) -> tuple[int, ...]:
    # Unused by jetlink; kept so a caller gets the model's own queue shape.
    return tuple(self.input_shapes[STATE_FEAT])

  @property
  def desire_q_shape(self) -> tuple[int, ...]:
    return tuple(self.input_shapes[STATE_DESIRE])
