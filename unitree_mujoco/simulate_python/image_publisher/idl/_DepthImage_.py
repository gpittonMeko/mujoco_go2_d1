from dataclasses import dataclass

import cyclonedds.idl as idl
import cyclonedds.idl.annotations as annotate
import cyclonedds.idl.types as types

@dataclass
@annotate.final
@annotate.autoid("sequential")
class DepthImage_(idl.IdlStruct, typename="unitree_mujoco.DepthImage_"):
    height: types.uint16
    width: types.uint16
    data: types.array[types.float32, 80 * 60]
