from unitree_sdk2py.core.channel import ChannelPublisher
from .idl._DepthImage_ import DepthImage_
from unitree_sdk2py.utils.thread import RecurrentThread

TOPIC_DEPTH_IMAGE = "rt/depthimage"

class DepthImagePublisher:
    def __init__(self, sim_image_data, image_publish_dt):
        self.sim_image_data = sim_image_data
        height = sim_image_data.shape[0]
        width = sim_image_data.shape[1]
        self.depth_image = DepthImage_(height, width, [0.0] * (height * width))
        self.publisher = ChannelPublisher(TOPIC_DEPTH_IMAGE, DepthImage_)
        self.publisher.Init()
        self.thread = RecurrentThread(
            interval=image_publish_dt, target=self.PublishDepthImage, name="sim_depthimage"
        )
        self.thread.Start()

    def PublishDepthImage(self):
        height = self.sim_image_data.shape[0]
        width = self.sim_image_data.shape[1]
        self.depth_image.height = height
        self.depth_image.width = width
        flat_data = self.sim_image_data.flatten()
        for i in range(height * width):
            self.depth_image.data[i] = float(flat_data[i])
        self.publisher.Write(self.depth_image)
