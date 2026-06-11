---
name: Example lab skill
description: Replace this folder with your real procedures; delete or rename example_lab when done.
---

## Example (safe defaults)

- Prefer **English** short instructions for `openvla.instruction_en`.
- For small arm nudges without a full plan, use **`arm_joint_delta`** with small `|delta_deg|` unless the operator asks for more.
- Never emit **`damping`** / **`velocity`** in `base_motion` unless the operator UI explicitly enabled those caps.
