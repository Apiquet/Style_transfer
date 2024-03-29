#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Style Transfer with VGG16:
Style layers = first 6 layers
Content layer = conv5_2
"""

import sys

import cv2
import imageio
import numpy as np
import tensorflow as tf
from PIL import Image, ImageDraw
from tqdm import tqdm


class VGG16StyleTransfer(tf.keras.Model):
    def __init__(self, tracker_ssd_path, ssd_weights_path=None, n_classes=21, floatType=32):
        """
        Args:
            - (str) tracker_ssd_path: path to github/Apiquet/Tracking_SSD_ReID
            - (str) ssd_weights_path: got from Tracking_SSD_ReID/training.ipynb
            - (int) n_classes: number of target classes
            - (int) floatType: if wanted to se float32 or 16
        """
        super(VGG16StyleTransfer, self).__init__()

        if floatType == 32:
            self.floatType = tf.float32
        elif floatType == 16:
            tf.keras.backend.set_floatx('float16')
            self.floatType = tf.float16
        else:
            raise Exception('floatType should be either 32 or 16')

        sys.path.append(tracker_ssd_path)
        from models.SSD300 import SSD300

        self.n_classes = n_classes

        # get SSD300 model
        SSD300_model = SSD300(21, floatType)
        input_shape = (300, 300, 3)
        # run model on zero tensor to initialize it
        confs, locs = SSD300_model(tf.zeros([32, 300, 300, 3], self.floatType))
        if ssd_weights_path is not None:
            SSD300_model.load_weights(ssd_weights_path)
        # get the VGG-16 part from SSD300
        SSD_backbone = SSD300_model.getVGG16()

        # get a new VGG-16 model until the stage 5
        from models.VGG16 import VGG16

        self.input_res = input_shape
        self.VGG16 = VGG16(input_shape=input_shape)
        self.VGG16_tilStage5 = self.VGG16.getUntilStage5()

        # load weights from SSD to new VGG-16 model
        ssd_seq_idx = 0
        ssd_layer_idx = 0
        for i in range(len(self.VGG16_tilStage5.layers)):
            ssd_layer_idx = i
            if i >= 13:
                ssd_seq_idx = 1
                ssd_layer_idx -= 13
            self.VGG16_tilStage5.get_layer(index=i).set_weights(
                SSD_backbone.get_layer(index=ssd_seq_idx)
                .get_layer(index=ssd_layer_idx)
                .get_weights()
            )
            self.VGG16_tilStage5.get_layer(index=i).trainable = False
        # del models that we won't use anymore
        del SSD_backbone
        del SSD300_model

        self.style_layers = []

        self.inputs = tf.keras.layers.Input(shape=input_shape)
        self.x = self.VGG16_tilStage5.get_layer(index=0)(self.inputs)

        for i in range(1, 7):
            # get first 6 layers for the style loss
            self.style_layers.append(self.x)
            self.x = self.VGG16_tilStage5.get_layer(index=i)(self.x)

        for i in range(7, len(self.VGG16_tilStage5.layers) - 2):
            self.x = self.VGG16_tilStage5.get_layer(index=i)(self.x)

        # get first layer at index -2 for the content loss
        self.content_layers = [self.x]

        self.model = tf.keras.Model(
            inputs=self.inputs, outputs=self.style_layers + self.content_layers
        )
        self.model.trainable = False

    def get_features(self, data):
        """
        Method to get model outputs for style and content layers used by loss

        Args:
            - (tf.Tensor) input model data with shape (1, 300, 300, 3)
        """
        outputs = self.model(data / 255.0)

        def gram_calc(data):
            return tf.linalg.einsum('bijc,bijd->bcd', data, data) / tf.cast(
                data.shape[1] * data.shape[2], tf.float32
            )

        style_features = [gram_calc(layer) for layer in outputs[:-1]]
        return style_features, outputs[-1]

    def get_loss(self, style_target, style_feature, content_target, content_feature):
        """
        Method to get loss value from style and content targets and features

        Args:
            - (list of tf.Tensor) style_target with shape (N, 1, W, H)
            with N the number of style layers
            - (list of tf.Tensor) style_feature with shape (N, 1, W, H)
            with N the number of style layers
            - (tf.Tensor) content_target with shape (1, W, H, C)
            - (tf.Tensor) content_feature with shape (1, W, H, C)
        """
        style_loss = tf.add_n(
            [
                tf.reduce_mean(tf.square(features - targets))
                for features, targets in zip(style_feature, style_target)
            ]
        )
        content_loss = tf.add_n([0.5 * tf.reduce_sum(tf.square(content_feature - content_target))])

        return 1 * style_loss + 1e-30 * content_loss

    def training(self, style_image, content_image, optimizer, epochs=1):
        """
        Method to apply style transfer on content image

        Args:
            - (tf.Tensor) style_image with shape (1, W, H, 3)
            - (tf.Tensor) content_image with shape (1, W, H, 3)
            - (tf.keras.optimizers) Optimizer to use
            - (int) number of epoch
        """
        images = []

        # infer the model on the style image to get the style targets
        # (result of the first 6 layers)
        style_targets, _ = self.get_features(style_image)

        # infer the model on the content image to get the content targets
        # (result of the layer of index -2)
        _, content_targets = self.get_features(content_image)

        # generate a copy of our content image
        # this copy will be update with the gradients over the epochs
        generated_image = tf.cast(content_image, dtype=tf.float32)
        generated_image = tf.Variable(generated_image)

        images.append(tf.keras.preprocessing.image.array_to_img(tf.squeeze(content_image)))

        # training loop
        for n in tqdm(range(epochs), position=0, leave=True):
            with tf.GradientTape() as tape:
                # run the model on the current image
                # (image updated at each run)
                # get the style feature (outputs of the first 5 layers)
                # and content feature (outputs of the layer with index -2)
                style_features, content_features = self.get_features(generated_image)
                # calculate the loss
                loss = self.get_loss(
                    style_targets, style_features, content_targets, content_features
                )

            # get gradients
            gradients = tape.gradient(loss, generated_image)
            # apply gradients wrt the image to update
            optimizer.apply_gradients([(gradients, generated_image)])
            # clip image to have a range of [0, 255]
            generated_image.assign(
                tf.clip_by_value(generated_image, clip_value_min=0.0, clip_value_max=255.0)
            )

            tmp_img = tf.Variable(generated_image)
            images.append(tf.keras.preprocessing.image.array_to_img(tf.squeeze(tmp_img)))
        return images

    def call(self, style_image, content_image, optimizer, epochs=1):
        return self.training(style_image, content_image, optimizer, epochs)

    def inferOnVideo(
        self,
        style_image_path,
        optimizer,
        epochs,
        video_path,
        out_path,
        start_idx=0,
        end_idx=-1,
        skip=1,
        resize=None,
        fps=30,
        add_content_img=False,
        add_style_img=False,
        line_width=2,
    ):
        """
        Method to infer model on a MP4 video
        Create a gif with the results

        Args:
            - (str) style_image_path: path to the style image
            - (tf.keras.optimizers) Optimizer to use
            - (int) number of epoch
            - (str) video path (MP4)
            - (str) out_gif: output path (.gif)
            - (int) start_idx: start frame idx, default is 0
            - (int) end_idx: end frame idx, default is -1
            - (int) skip: idx%skip != 0 is skipped
            - (tuple) resize: target resolution for the gif
            - (int) fps: fps of the output gif
            - (bool) add_content_img: add content image on bottom left
            - (bool) add_style_img: add style image on bottom left of result
        """
        style_image = Image.open(style_image_path)
        style_image = np.array(style_image)
        style_image = cv2.resize(style_image, (300, 300), interpolation=cv2.INTER_NEAREST)
        style_image = tf.expand_dims(tf.convert_to_tensor(style_image, dtype=tf.float32), 0)

        style_image_on_gif = tf.keras.preprocessing.image.array_to_img(tf.squeeze(style_image))

        cap = cv2.VideoCapture(video_path)
        imgs = []
        i = 0
        number_of_frame = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if end_idx != -1:
            number_of_frame = end_idx

        for _ in tqdm(range(number_of_frame), position=0, leave=True):
            ret, frame = cap.read()
            if not ret:
                break
            i += 1
            if i <= start_idx:
                continue
            elif end_idx >= 0 and i > end_idx:
                break
            if i % skip != 0:
                continue
            img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            orig_height, orig_width = img.size[0], img.size[1]
            if resize is None:
                resize = (img.size[0], img.size[1])

            content_image = np.array(img)
            content_image = cv2.resize(content_image, (300, 300), interpolation=cv2.INTER_NEAREST)
            content_image = tf.expand_dims(tf.convert_to_tensor(content_image, dtype=tf.float32), 0)

            image_result = self.training(style_image, content_image, optimizer, epochs)[-1]

            image_result = image_result.resize(resize, Image.ANTIALIAS)

            # add content image on final gif
            if add_content_img and not add_style_img:
                min_img_size = (resize[0] // 3, resize[1] // 3)
                pil_content = img.resize(min_img_size, Image.ANTIALIAS)
                draw = ImageDraw.Draw(pil_content)
                min_point = (-10, 0)
                end_point = (pil_content.size[0] - 3, pil_content.size[1] + 10)
                draw.rectangle((min_point, end_point), outline=(255, 255, 255), width=line_width)
                image_result.paste(pil_content, (0, image_result.size[1] - pil_content.size[1]))

            # add style image on final gif
            if not add_content_img and add_style_img:
                style_resize_ratio = int(round(orig_height / resize[0] * 0.8))
                pil_style = style_image_on_gif.resize(
                    (
                        int(style_image_on_gif.size[0] // style_resize_ratio),
                        int(style_image_on_gif.size[1] // style_resize_ratio),
                    ),
                    Image.ANTIALIAS,
                )
                draw = ImageDraw.Draw(pil_style)
                min_point = (-10, 0)
                end_point = (pil_style.size[0] - 3, pil_style.size[1] + 10)
                draw.rectangle((min_point, end_point), outline=(255, 255, 255), width=line_width)
                image_result.paste(pil_style, (0, image_result.size[1] - pil_style.size[1]))

            # add content and style images on final gif
            if add_content_img and add_style_img:
                min_img_size = (resize[0] // 3, resize[1] // 3)
                pil_content = img.resize(min_img_size, Image.ANTIALIAS)
                draw = ImageDraw.Draw(pil_content)
                min_point = (-10, 0)
                end_point = (pil_content.size[0] - 3, pil_content.size[1] + 10)
                draw.rectangle((min_point, end_point), outline=(255, 255, 255), width=line_width)
                image_result.paste(pil_content, (0, image_result.size[1] - pil_content.size[1]))

                style_resize_ratio = int(round(orig_height / resize[0] * 0.8))
                pil_style = style_image_on_gif.resize(
                    (
                        int(style_image_on_gif.size[0] // style_resize_ratio),
                        int(style_image_on_gif.size[1] // style_resize_ratio),
                    ),
                    Image.ANTIALIAS,
                )
                draw = ImageDraw.Draw(pil_style)
                min_point = (-10, 0)
                end_point = (pil_style.size[0] - 3, pil_style.size[1] + 10)
                draw.rectangle((min_point, end_point), outline=(255, 255, 255), width=line_width)
                ypos_offset = pil_content.size[1] + pil_style.size[1]
                image_result.paste(pil_style, (0, image_result.size[1] - ypos_offset))

            imgs.append(image_result)

        imgs[0].save(out_path, format='GIF', append_images=imgs[1:], save_all=True, loop=0)
        gif = imageio.mimread(out_path)
        imageio.mimsave(out_path, gif, fps=fps)

    def inferOnImage(
        self,
        style_image_path,
        optimizer,
        epochs,
        image_path,
        out_path,
        resize=None,
        add_content_img=False,
        add_style_img=False,
        line_width=2,
    ):
        """
        Method to infer model on a MP4 video
        Create a gif with the results

        Args:
            - (str) style_image_path: path to the style image
            - (tf.keras.optimizers) Optimizer to use
            - (int) number of epoch
            - (str) image path: path the content image
            - (str) out_path: path to save the result
            - (tuple) resize: target resolution for the gif
            - (bool) add_content_img: add the content image on bottom left
            - (bool) add_style_img: add style image on bottom left of result
        """
        content_image_init = Image.open("imgs/content.jpg")
        orig_height, orig_width = content_image_init.size[0], content_image_init.size[1]
        if resize is None:
            resize = (orig_height, orig_width)
        content_image = np.array(content_image_init)
        content_image = cv2.resize(content_image, (300, 300), interpolation=cv2.INTER_NEAREST)
        content_image = tf.expand_dims(tf.convert_to_tensor(content_image, dtype=tf.float32), 0)

        style_image = Image.open(style_image_path)
        style_image = np.array(style_image)
        style_image = cv2.resize(style_image, (300, 300), interpolation=cv2.INTER_NEAREST)
        style_image = tf.expand_dims(tf.convert_to_tensor(style_image, dtype=tf.float32), 0)

        style_image_on_gif = tf.keras.preprocessing.image.array_to_img(tf.squeeze(style_image))

        image_result = self.training(style_image, content_image, optimizer, epochs)[-1]

        image_result = image_result.resize(resize, Image.ANTIALIAS)

        # add content image on final gif
        if add_content_img and not add_style_img:
            min_img_size = (resize[0] // 3, resize[1] // 3)
            pil_content = content_image_init.resize(min_img_size, Image.ANTIALIAS)
            draw = ImageDraw.Draw(pil_content)
            min_point = (-10, 0)
            end_point = (pil_content.size[0] - 3, pil_content.size[1] + 10)
            draw.rectangle((min_point, end_point), outline=(255, 255, 255), width=line_width)
            image_result.paste(pil_content, (0, image_result.size[1] - pil_content.size[1]))

        # add style image on final gif
        if not add_content_img and add_style_img:
            style_resize_ratio = int(round(orig_height / resize[0] * 0.8))
            pil_style = style_image_on_gif.resize(
                (
                    int(style_image_on_gif.size[0] // style_resize_ratio),
                    int(style_image_on_gif.size[1] // style_resize_ratio),
                ),
                Image.ANTIALIAS,
            )
            draw = ImageDraw.Draw(pil_style)
            min_point = (-10, 0)
            end_point = (pil_style.size[0] - 3, pil_style.size[1] + 10)
            draw.rectangle((min_point, end_point), outline=(255, 255, 255), width=line_width)
            image_result.paste(pil_style, (0, image_result.size[1] - pil_style.size[1]))

        # add content and style images on final gif
        if add_content_img and add_style_img:
            min_img_size = (resize[0] // 3, resize[1] // 3)
            pil_content = content_image_init.resize(min_img_size, Image.ANTIALIAS)
            draw = ImageDraw.Draw(pil_content)
            min_point = (-10, 0)
            end_point = (pil_content.size[0] - 3, pil_content.size[1] + 10)
            draw.rectangle((min_point, end_point), outline=(255, 255, 255), width=line_width)
            image_result.paste(pil_content, (0, image_result.size[1] - pil_content.size[1]))

            style_resize_ratio = int(round(orig_height / resize[0] * 0.8))
            pil_style = style_image_on_gif.resize(
                (
                    int(style_image_on_gif.size[0] // style_resize_ratio),
                    int(style_image_on_gif.size[1] // style_resize_ratio),
                ),
                Image.ANTIALIAS,
            )
            draw = ImageDraw.Draw(pil_style)
            min_point = (-10, 0)
            end_point = (pil_style.size[0] - 3, pil_style.size[1] + 10)
            draw.rectangle((min_point, end_point), outline=(255, 255, 255), width=line_width)
            ypos_offset = pil_content.size[1] + pil_style.size[1]
            image_result.paste(pil_style, (0, image_result.size[1] - ypos_offset))

        image_result.save(out_path)
