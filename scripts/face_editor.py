from operator import attrgetter
from typing import List

import cv2
import gradio as gr
import modules.images as images
import modules.scripts as scripts
import modules.shared as shared
import numpy as np
import torch
from facexlib.detection import RetinaFace, init_detection_model, retinaface
from facexlib.parsing import BiSeNet, init_parsing_model
from facexlib.utils.misc import img2tensor
from modules.processing import (Processed, StableDiffusionProcessing,
                                StableDiffusionProcessingImg2Img,
                                create_infotext, process_images)
from PIL import Image
from torchvision.transforms.functional import normalize


class Face:
    def __init__(self, entire_image: np.ndarray, face_box: np.ndarray, face_margin: float, face_size: int):
        left, top, right, bottom = self.__to_square(face_box)

        self.left, self.top, self.right, self.bottom = self.__ensure_margin(
            left, top, right, bottom, entire_image, face_margin)

        self.width = self.right - self.left
        self.height = self.bottom - self.top

        self.image = self.__crop_face_image(entire_image, face_size)

    def __crop_face_image(self, entire_image: np.ndarray, face_size: int):
        cropped = entire_image[self.top: self.bottom, self.left: self.right, :]
        return Image.fromarray(
            cv2.resize(cropped, dsize=(face_size, face_size)))

    def __to_square(self, face_box: np.ndarray):
        left, top, right, bottom, *_ = list(map(int, face_box))
        self.face_area = left, top, right, bottom
        self.center = right - int((right - left) / 2)

        width = right - left
        height = bottom - top

        if width % 2 == 1:
            right = right + 1
            width = width + 1
        if height % 2 == 1:
            bottom = bottom + 1
            height = height + 1

        diff = int(abs(width - height) / 2)
        if width > height:
            top = top - diff
            bottom = bottom + diff
        else:
            left = left - diff
            right = right + diff

        return left, top, right, bottom

    def __ensure_margin(self, left: int, top: int, right: int, bottom: int, entire_image: np.ndarray, margin: float):
        entire_height, entire_width = entire_image.shape[:2]

        side_length = right - left
        margin = min(min(entire_height, entire_width) /
                     side_length, margin)
        diff = int((side_length * margin - side_length) / 2)

        top = top - diff
        bottom = bottom + diff
        left = left - diff
        right = right + diff

        if top < 0:
            bottom = bottom - top
            top = 0
        if left < 0:
            right = right - left
            left = 0

        if bottom > entire_height:
            top = top - (bottom - entire_height)
            bottom = entire_height
        if right > entire_width:
            left = left - (right - entire_width)
            right = entire_width

        return left, top, right, bottom


class Script(scripts.Script):
    def __init__(self) -> None:
        super().__init__()

    def title(self):
        return "Face Editor"

    def show(self, is_img2img):
        return True

    def ui(self, is_img2img):
        self.components = []

        use_minimal_area = gr.Checkbox(
            label="Use minimal area for face selection (for multiple faces)",
            value=False)
        self.components.append((use_minimal_area, self.add_prefix("use_minimal_area")))

        save_original_image = gr.Checkbox(
            label="Save original image",
            value=False
        )
        self.components.append((save_original_image, self.add_prefix("save_original_image")))

        show_intermediate_steps = gr.Checkbox(
            label="Show intermediate steps",
            value=False)
        self.components.append((show_intermediate_steps, self.add_prefix("show_intermediate_steps")))

        prompt_for_face = gr.Textbox(
            show_label=False,
            placeholder="Prompt for face",
            label="Prompt for face",
            lines=2,
        )
        self.components.append((prompt_for_face, self.add_prefix("prompt_for_face")))

        mask_size = gr.Slider(label="Mask size", minimum=0,
                              maximum=64, step=1, value=0)
        self.components.append((mask_size, self.add_prefix("mask_size")))

        mask_blur = gr.Slider(label="Mask blur ", minimum=0,
                              maximum=64, step=1, value=12)
        self.components.append((mask_blur, self.add_prefix("mask_blur")))

        with gr.Accordion("Advanced Options", open=False):
            with gr.Accordion("(1) Face Detection", open=False):
                max_face_count = gr.Slider(
                    minimum=1,
                    maximum=20,
                    step=1,
                    value=20,
                    label="Maximum number of faces to detect",
                )
                self.components.append((max_face_count, self.add_prefix("max_face_count")))

                confidence = gr.Slider(
                    minimum=0.7,
                    maximum=1.0,
                    step=0.01,
                    value=0.97,
                    label="Face detection confidence",
                )
                self.components.append((confidence, self.add_prefix("confidence")))

            with gr.Accordion("(2) Crop and Resize the Faces", open=False):
                face_margin = gr.Slider(
                    minimum=1.0, maximum=2.0, step=0.1, value=1.6, label="Face margin"
                )
                self.components.append((face_margin, self.add_prefix("face_margin")))

                face_size = gr.Slider(label="Size of the face when recreating",
                                      minimum=64, maximum=2048, step=16, value=512)
                self.components.append((face_size, self.add_prefix("face_size")))

                ignore_larger_faces = gr.Checkbox(
                    label="Ignore faces larger than specified size",
                    value=True
                )
                self.components.append((ignore_larger_faces, self.add_prefix("ignore_larger_faces")))

            with gr.Accordion("(3) Recreate the Faces", open=False):
                strength1 = gr.Slider(
                    minimum=0.1,
                    maximum=0.8,
                    step=0.05,
                    value=0.4,
                    label="Denoising strength for face images",
                )
                self.components.append((strength1, self.add_prefix("strength1")))

                apply_scripts_to_faces = gr.Checkbox(
                    label="Apply scripts to faces",
                    visible=False,
                    value=False)
                self.components.append((apply_scripts_to_faces, self.add_prefix("apply_scripts_to_faces")))

            with gr.Accordion("(4) Paste the Faces", open=False):
                apply_inside_mask_only = gr.Checkbox(
                    label="Apply inside mask only ",
                    value=True
                )
                self.components.append((apply_inside_mask_only, self.add_prefix("apply_inside_mask_only")))

            with gr.Accordion("(5) Blend the entire image", open=False):
                strength2 = gr.Slider(
                    minimum=0.0,
                    maximum=1.0,
                    step=0.05,
                    value=0.0,
                    label="Denoising strength for the entire image ",
                )
                self.components.append((strength2, self.add_prefix("strength2")))

        return [
            face_margin,
            confidence,
            strength1,
            strength2,
            max_face_count,
            mask_size,
            mask_blur,
            prompt_for_face,
            apply_inside_mask_only,
            save_original_image,
            show_intermediate_steps,
            apply_scripts_to_faces,
            face_size,
            use_minimal_area,
            ignore_larger_faces,
        ]

    def get_face_models(self):
        if hasattr(retinaface, 'device'):
            retinaface.device = shared.device

        mask_model = init_parsing_model(device=shared.device)
        detection_model = init_detection_model(
            "retinaface_resnet50", device=shared.device
        )
        return (mask_model, detection_model)

    def run(
        self,
        o: StableDiffusionProcessing,
        face_margin: float,
        confidence: float,
        strength1: float,
        strength2: float,
        max_face_count: int,
        mask_size: int,
        mask_blur: int,
        prompt_for_face: str,
        apply_inside_mask_only: bool,
        save_original_image: bool,
        show_intermediate_steps: bool,
        apply_scripts_to_faces: bool,
        face_size: int,
        use_minimal_area: bool,
        ignore_larger_faces: bool,
    ):

        mask_model, detection_model = self.get_face_models()

        if isinstance(o, StableDiffusionProcessingImg2Img) and o.n_iter == 1 and o.batch_size == 1 and not apply_scripts_to_faces:
            return self.__proc_image(o, mask_model, detection_model,
                                     face_margin=face_margin, confidence=confidence,
                                     strength1=strength1, strength2=strength2,
                                     max_face_count=max_face_count, mask_size=mask_size,
                                     mask_blur=mask_blur, prompt_for_face=prompt_for_face,
                                     apply_inside_mask_only=apply_inside_mask_only,
                                     show_intermediate_steps=show_intermediate_steps,
                                     apply_scripts_to_faces=apply_scripts_to_faces,
                                     face_size=face_size,
                                     use_minimal_area=use_minimal_area,
                                     ignore_larger_faces=ignore_larger_faces)
        else:
            shared.state.job_count = o.n_iter * 3
            if not save_original_image:
                o.do_not_save_samples = True
            res = process_images(o)
            o.do_not_save_samples = False

            return self.proc_images(mask_model, detection_model, o, res,
                                    face_margin=face_margin, confidence=confidence,
                                    strength1=strength1, strength2=strength2,
                                    max_face_count=max_face_count, mask_size=mask_size,
                                    mask_blur=mask_blur, prompt_for_face=prompt_for_face,
                                    apply_inside_mask_only=apply_inside_mask_only,
                                    show_intermediate_steps=show_intermediate_steps,
                                    apply_scripts_to_faces=apply_scripts_to_faces,
                                    face_size=face_size,
                                    use_minimal_area=use_minimal_area,
                                    ignore_larger_faces=ignore_larger_faces)

    def proc_images(
        self,
        mask_model: BiSeNet,
        detection_model: RetinaFace,
        o: StableDiffusionProcessing,
        res: Processed,
        face_margin: float,
        confidence: float,
        strength1: float,
        strength2: float,
        max_face_count: int,
        mask_size: int,
        mask_blur: int,
        prompt_for_face: str,
        apply_inside_mask_only: bool,
        show_intermediate_steps: bool,
        apply_scripts_to_faces: bool,
        face_size: int,
        use_minimal_area: bool,
        ignore_larger_faces: bool,
    ):
        edited_images, all_seeds, all_prompts, infotexts = [], [], [], []
        seed_index = 0
        subseed_index = 0

        self.__extend_infos(res.all_prompts, len(res.images))
        self.__extend_infos(res.all_seeds, len(res.images))
        self.__extend_infos(res.infotexts, len(res.images))

        for i, image in enumerate(res.images):
            if i < res.index_of_first_image:
                continue

            p = StableDiffusionProcessingImg2Img(init_images=[image])
            self.__init_processing(p, o, image)

            if seed_index < len(res.all_seeds):
                p.seed = res.all_seeds[seed_index]
                seed_index += 1
            if subseed_index < len(res.all_subseeds):
                p.subseed = res.all_subseeds[subseed_index]
                subseed_index += 1
            proc = self.__proc_image(p, mask_model, detection_model,
                                     face_margin=face_margin, confidence=confidence,
                                     strength1=strength1, strength2=strength2,
                                     max_face_count=max_face_count, mask_size=mask_size,
                                     mask_blur=mask_blur, prompt_for_face=prompt_for_face,
                                     apply_inside_mask_only=apply_inside_mask_only,
                                     pre_proc_image=image,
                                     show_intermediate_steps=show_intermediate_steps,
                                     apply_scripts_to_faces=apply_scripts_to_faces,
                                     face_size=face_size,
                                     use_minimal_area=use_minimal_area,
                                     ignore_larger_faces=ignore_larger_faces)
            edited_images.extend(proc.images)
            all_seeds.extend(proc.all_seeds)
            all_prompts.extend(proc.all_prompts)
            infotexts.extend(proc.infotexts)

        res.images.extend(edited_images)
        res.all_seeds.extend(all_seeds)
        res.all_prompts.extend(all_prompts)
        res.infotexts.extend(infotexts)
        return res

    def __init_processing(self, p: StableDiffusionProcessingImg2Img, o: StableDiffusionProcessing, image):
        sample = p.sample
        p.__dict__.update(o.__dict__)
        p.sampler = None
        p.c = None
        p.uc = None
        p.cached_c = [None, None]
        p.cached_uc = [None, None]
        p.init_images = [image]
        p.width, p.height = image.size
        p.sample = sample

    def __proc_image(self, p: StableDiffusionProcessingImg2Img,
                     mask_model: BiSeNet,
                     detection_model: RetinaFace,
                     face_margin: float,
                     confidence: float,
                     strength1: float,
                     strength2: float,
                     max_face_count: int,
                     mask_size: int,
                     mask_blur: int,
                     prompt_for_face: str,
                     apply_inside_mask_only: bool,
                     pre_proc_image: Image = None,
                     show_intermediate_steps: bool = False,
                     apply_scripts_to_faces: bool = False,
                     face_size: int = 512,
                     use_minimal_area: bool = False,
                     ignore_larger_faces: bool = True) -> Processed:
        params = {
            self.add_prefix("enabled"): True,
            self.add_prefix("face_margin"): face_margin,
            self.add_prefix("confidence"): confidence,
            self.add_prefix("strength1"): strength1,
            self.add_prefix("strength2"): strength2,
            self.add_prefix("max_face_count"): max_face_count,
            self.add_prefix("mask_size"): mask_size,
            self.add_prefix("mask_blur"): mask_blur,
            self.add_prefix("prompt_for_face"): prompt_for_face if len(prompt_for_face) > 0 else '""',
            self.add_prefix("apply_inside_mask_only"): apply_inside_mask_only,
            self.add_prefix("apply_scripts_to_faces"): apply_scripts_to_faces,
            self.add_prefix("face_size"): face_size,
            self.add_prefix("use_minimal_area"): use_minimal_area,
            self.add_prefix("ignore_larger_faces"): ignore_larger_faces,
        }

        apply_scripts_to_faces = False

        if hasattr(p.init_images[0], 'mode') and p.init_images[0].mode != 'RGB':
            p.init_images[0] = p.init_images[0].convert('RGB')

        entire_image = np.array(p.init_images[0])
        faces = self.__crop_face(
            detection_model, p.init_images[0], face_margin, confidence, face_size, ignore_larger_faces)
        faces = faces[:max_face_count]
        faces = sorted(faces, key=attrgetter("center"))
        entire_mask_image = np.zeros_like(entire_image)

        entire_width = (p.width // 8) * 8
        entire_height = (p.height // 8) * 8
        entire_prompt = p.prompt
        entire_all_prompts = p.all_prompts
        p.batch_size = 1
        p.n_iter = 1

        if shared.state.job_count == -1:
            shared.state.job_count = len(faces) + 1

        print(f"number of faces: {len(faces)}")
        if len(faces) == 0 and pre_proc_image is not None:
            return Processed(p, images_list=[pre_proc_image], all_prompts=[p.prompt], all_seeds=[p.seed], infotexts=[""])
        output_images = []

        wildcards_script = None
        for script in p.scripts.alwayson_scripts:
            if script.filename.endswith("stable-diffusion-webui-wildcards/scripts/wildcards.py"):
                wildcards_script = script
        face_prompts = self.__get_face_prompts(len(faces), prompt_for_face, entire_prompt)
        face_prompt_index = 0

        if not apply_scripts_to_faces:
            p.scripts = None

        for face in faces:
            if shared.state.interrupted:
                break

            p.init_images = [face.image]
            p.width = face.image.width
            p.height = face.image.height
            p.denoising_strength = strength1
            p.prompt = face_prompts[face_prompt_index]
            if wildcards_script is not None:
                p.prompt = self.__apply_wildcards(wildcards_script, p.prompt, face_prompt_index)
            face_prompt_index += 1
            print(f"prompt for the face: {p.prompt}")

            p.do_not_save_samples = True

            proc = process_images(p)

            if proc.images[0].mode != 'RGB':
                proc.images[0] = proc.images[0].convert('RGB')

            face_image = np.array(proc.images[0])
            mask_image = self.__to_mask_image(
                mask_model, face_image, mask_size)

            if mask_blur > 0:
                mask_image = cv2.blur(mask_image, (mask_blur, mask_blur))

            if show_intermediate_steps:
                feature = self.__get_feature(p.prompt, entire_prompt)
                mask_info = f"size:{mask_size}, blur:{mask_blur}"
                output_images.append(Image.fromarray(self.__add_comment(face_image, feature)))
                output_images.append(Image.fromarray(self.__add_comment(self.__to_masked_image(mask_image, face_image), mask_info)))

            face_image = cv2.resize(face_image, dsize=(
                face.width, face.height))
            mask_image = cv2.resize(mask_image, dsize=(
                face.width, face.height))

            if use_minimal_area:
                l, t, r, b = face.face_area
                face_image = face_image[t - face.top: b - face.top, l - face.left: r - face.left]
                mask_image = mask_image[t - face.top: b - face.top, l - face.left: r - face.left]
                face.top = t
                face.left = l
                face.bottom = b
                face.right = r

            if apply_inside_mask_only:
                face_background = entire_image[
                    face.top: face.bottom,
                    face.left: face.right,
                ]
                face_fg = (face_image * (mask_image/255.0)).astype('uint8')
                face_bg = (face_background *
                           (1 - (mask_image/255.0))).astype('uint8')
                face_image = face_fg + face_bg

            entire_image[
                face.top: face.bottom,
                face.left: face.right,
            ] = face_image
            entire_mask_image[
                face.top: face.bottom,
                face.left: face.right,
            ] = mask_image

        p.prompt = entire_prompt
        p.all_prompts = entire_all_prompts
        p.width = entire_width
        p.height = entire_height
        p.init_images = [Image.fromarray(entire_image)]
        p.denoising_strength = strength2
        p.mask_blur = mask_blur
        p.inpainting_mask_invert = 1
        p.inpainting_fill = 1
        p.image_mask = Image.fromarray(entire_mask_image)
        p.do_not_save_samples = False

        p.extra_generation_params.update(params)

        if p.denoising_strength > 0:
            proc = process_images(p)
        else:
            proc = self.__save_images(p)

        if show_intermediate_steps:
            output_images.append(p.init_images[0])
            output_images.append(Image.fromarray(
                self.__to_masked_image(entire_mask_image, entire_image)))
            output_images.append(proc.images[0])
            proc.images = output_images

        self.__extend_infos(proc.all_prompts, len(proc.images))
        self.__extend_infos(proc.all_seeds, len(proc.images))
        self.__extend_infos(proc.infotexts, len(proc.images))

        return proc

    def __get_feature(self, prompt: str, entire_prompt: str) -> str:
        if prompt == "" or prompt == entire_prompt:
            return ""
        return prompt.replace(entire_prompt, "")

    def __add_comment(self, image: np.ndarray, comment: str) -> np.ndarray:
        image = np.copy(image)
        h, _, _ = image.shape
        cv2.putText(image, text=comment, org=(10, h - 16), fontFace=cv2.FONT_HERSHEY_SIMPLEX, fontScale=1.2, color=(0, 0, 0), thickness=10)
        cv2.putText(image, text=comment, org=(10, h - 16), fontFace=cv2.FONT_HERSHEY_SIMPLEX, fontScale=1.2, color=(255, 255, 255), thickness=2)
        return image

    def __apply_wildcards(self, wildcards_script: scripts.Script, prompt: str, seed: int) -> str:
        if "__" in prompt:
            wp = StableDiffusionProcessing()
            wp.all_prompts = [prompt]
            wp.all_seeds = [0 if shared.opts.wildcards_same_seed else seed]
            wildcards_script.process(wp)
            return wp.all_prompts[0]
        return prompt

    def __get_face_prompts(self, length: int, prompt_for_face: str, entire_prompt: str) -> List[str]:
        if len(prompt_for_face) == 0:
            return [entire_prompt] * length
        prompts = []
        p = prompt_for_face.split("||")
        for i in range(length):
            if i >= len(p):
                i = 0
            prompts.append(self.__edit_face_prompt(p[i], p[0], entire_prompt))
        return prompts

    def __edit_face_prompt(self, prompt: str, default_prompt: str, entire_prompt: str) -> str:
        if len(prompt) == 0:
            return default_prompt

        return prompt.strip().replace("@@", entire_prompt)

    def __save_images(self, p: StableDiffusionProcessingImg2Img) -> Processed:
        if p.all_prompts is None or len(p.all_prompts) == 0:
            p.all_prompts = [p.prompt]
        infotext = create_infotext(p, p.all_prompts, p.all_seeds, p.all_subseeds, {}, 0, 0)
        images.save_image(p.init_images[0], p.outpath_samples, "", p.seed, p.prompt, shared.opts.samples_format, info=infotext, p=p)
        return Processed(p, images_list=p.init_images, seed=p.seed,
                         info=infotext, subseed=p.subseed, index_of_first_image=0, infotexts=[infotext])

    def add_prefix(self, text: str) -> str:
        return "face_editor_" + text

    def __extend_infos(self, infos: list, image_count: int):
        return infos.extend([infos[0]] * (image_count - len(infos)))

    def __to_masked_image(self, mask_image: np.ndarray, image: np.ndarray) -> np.ndarray:
        gray_mask = np.where(mask_image == 0, 47, 255) / 255.0
        return (image * gray_mask).astype('uint8')

    def __crop_face(self, detection_model: RetinaFace, image: Image, face_margin: float, confidence: float,
                    face_size: int, ignore_larger_faces: bool) -> list:
        with torch.no_grad():
            face_boxes, _ = detection_model.align_multi(image, confidence)
            return self.__crop(image, face_boxes, face_margin, face_size, ignore_larger_faces)

    def __crop(self, image: Image, face_boxes: list, face_margin: float, face_size: int, ignore_larger_faces: bool) -> list:
        image = np.array(image, dtype=np.uint8)

        areas = []
        for face_box in face_boxes:
            face = Face(image, face_box, face_margin, face_size)
            if ignore_larger_faces and face.width > face_size:
                continue
            areas.append(face)

        return sorted(areas, key=attrgetter("height"), reverse=True)

    def __to_mask_image(self, mask_model: BiSeNet, face_image: Image, mask_size: int) -> np.ndarray:
        face_image = np.array(face_image)
        h, w, _ = face_image.shape

        if w != 512 or h != 512:
            rw = (int(w * (512 / w)) // 8) * 8
            rh = (int(h * (512 / h)) // 8) * 8
            face_image = cv2.resize(face_image, dsize=(rw, rh))

        face_tensor = img2tensor(face_image.astype(
            "float32") / 255.0, float32=True)
        normalize(face_tensor, (0.5, 0.5, 0.5), (0.5, 0.5, 0.5), inplace=True)
        face_tensor = torch.unsqueeze(face_tensor, 0).to(shared.device)

        with torch.no_grad():
            face = mask_model(face_tensor)[0]
        face = face.squeeze(0).cpu().numpy().argmax(0)
        face = face.copy().astype(np.uint8)

        mask = self.__to_mask(face)
        if mask_size > 0:
            mask = cv2.dilate(mask, np.empty(
                0, np.uint8), iterations=mask_size)

        if w != 512 or h != 512:
            mask = cv2.resize(mask, dsize=(w, h))

        return mask

    def __to_mask(self, face: np.ndarray) -> np.ndarray:
        mask = np.zeros((face.shape[0], face.shape[1], 3), dtype=np.uint8)
        num_of_class = np.max(face)
        for i in range(1, num_of_class + 1):
            index = np.where(face == i)
            if i < 14:
                mask[index[0], index[1], :] = [255, 255, 255]
        return mask
