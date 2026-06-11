# Vendored minimal `grasp_gen` namespace for the worker container.
# Only the lightweight ZMQ client (grasp_gen.serving.zmq_client) is shipped here so the
# worker can talk to the GraspGen ZMQ server without pulling torch/CUDA.
