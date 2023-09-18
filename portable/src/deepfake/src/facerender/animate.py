import os
import cv2
import yaml
import numpy as np
import warnings
from skimage import img_as_ubyte

warnings.filterwarnings('ignore')


import imageio
import torch
import torchvision


from src.facerender.modules.keypoint_detector import HEEstimator, KPDetector
from src.facerender.modules.mapping import MappingNet
from src.facerender.modules.generator import OcclusionAwareGenerator, OcclusionAwareSPADEGenerator
from src.facerender.modules.make_animation import make_animation 

from pydub import AudioSegment 
from src.utils.face_enhancer import enhancer as face_enhancer
from src.utils.paste_pic import paste_pic
from src.utils.videoio import save_video_with_audio


class AnimateFromCoeff():

    def __init__(self, free_view_checkpoint, mapping_checkpoint,
                   config_path, device):

        with open(config_path) as f:
            config = yaml.safe_load(f)

        generator = OcclusionAwareSPADEGenerator(**config['model_params']['generator_params'],
                                                    **config['model_params']['common_params'])
        kp_extractor = KPDetector(**config['model_params']['kp_detector_params'],
                                    **config['model_params']['common_params'])
        he_estimator = HEEstimator(**config['model_params']['he_estimator_params'],
                               **config['model_params']['common_params'])
        mapping = MappingNet(**config['model_params']['mapping_params'])


        generator.to(device)
        kp_extractor.to(device)
        he_estimator.to(device)
        mapping.to(device)
        for param in generator.parameters():
            param.requires_grad = False
        for param in kp_extractor.parameters():
            param.requires_grad = False 
        for param in he_estimator.parameters():
            param.requires_grad = False
        for param in mapping.parameters():
            param.requires_grad = False

        if free_view_checkpoint is not None:
            self.load_cpk_facevid2vid(free_view_checkpoint, kp_detector=kp_extractor, generator=generator, he_estimator=he_estimator)
        else:
            raise AttributeError("Checkpoint should be specified for video head pose estimator.")

        if  mapping_checkpoint is not None:
            self.load_cpk_mapping(mapping_checkpoint, mapping=mapping)
        else:
            raise AttributeError("Checkpoint should be specified for video head pose estimator.") 

        self.kp_extractor = kp_extractor
        self.generator = generator
        self.he_estimator = he_estimator
        self.mapping = mapping

        self.kp_extractor.eval()
        self.generator.eval()
        self.he_estimator.eval()
        self.mapping.eval()
         
        self.device = device
    
    def load_cpk_facevid2vid(self, checkpoint_path, generator=None, discriminator=None, 
                        kp_detector=None, he_estimator=None, optimizer_generator=None, 
                        optimizer_discriminator=None, optimizer_kp_detector=None, 
                        optimizer_he_estimator=None, device="cpu"):
        checkpoint = torch.load(checkpoint_path, map_location=torch.device(device))
        if generator is not None:
            generator.load_state_dict(checkpoint['generator'])
        if kp_detector is not None:
            kp_detector.load_state_dict(checkpoint['kp_detector'])
        if he_estimator is not None:
            he_estimator.load_state_dict(checkpoint['he_estimator'])
        if discriminator is not None:
            try:
               discriminator.load_state_dict(checkpoint['discriminator'])
            except:
               print ('No discriminator in the state-dict. Dicriminator will be randomly initialized')
        if optimizer_generator is not None:
            optimizer_generator.load_state_dict(checkpoint['optimizer_generator'])
        if optimizer_discriminator is not None:
            try:
                optimizer_discriminator.load_state_dict(checkpoint['optimizer_discriminator'])
            except RuntimeError as e:
                print ('No discriminator optimizer in the state-dict. Optimizer will be not initialized')
        if optimizer_kp_detector is not None:
            optimizer_kp_detector.load_state_dict(checkpoint['optimizer_kp_detector'])
        if optimizer_he_estimator is not None:
            optimizer_he_estimator.load_state_dict(checkpoint['optimizer_he_estimator'])

        return checkpoint['epoch']
    
    def load_cpk_mapping(self, checkpoint_path, mapping=None, discriminator=None,
                 optimizer_mapping=None, optimizer_discriminator=None, device='cpu'):
        checkpoint = torch.load(checkpoint_path,  map_location=torch.device(device))
        if mapping is not None:
            mapping.load_state_dict(checkpoint['mapping'])
        if discriminator is not None:
            discriminator.load_state_dict(checkpoint['discriminator'])
        if optimizer_mapping is not None:
            optimizer_mapping.load_state_dict(checkpoint['optimizer_mapping'])
        if optimizer_discriminator is not None:
            optimizer_discriminator.load_state_dict(checkpoint['optimizer_discriminator'])

        return checkpoint['epoch']

    def generate(self, x, video_save_dir, pic_path, crop_info, enhancer=None, background_enhancer=None, preprocess='crop', pic_path_type="static"):

        source_image = x['source_image'].type(torch.FloatTensor)
        source_semantics = x['source_semantics'].type(torch.FloatTensor)
        target_semantics = x['target_semantics_list'].type(torch.FloatTensor)
        source_image = source_image.to(self.device)
        source_semantics = source_semantics.to(self.device)
        target_semantics = target_semantics.to(self.device)
        if 'yaw_c_seq' in x:
            yaw_c_seq = x['yaw_c_seq'].type(torch.FloatTensor)
            yaw_c_seq = x['yaw_c_seq'].to(self.device)
        else:
            yaw_c_seq = None
        if 'pitch_c_seq' in x:
            pitch_c_seq = x['pitch_c_seq'].type(torch.FloatTensor)
            pitch_c_seq = x['pitch_c_seq'].to(self.device)
        else:
            pitch_c_seq = None
        if 'roll_c_seq' in x:
            roll_c_seq = x['roll_c_seq'].type(torch.FloatTensor) 
            roll_c_seq = x['roll_c_seq'].to(self.device)
        else:
            roll_c_seq = None

        frame_num = x['frame_num']

        predictions_video = make_animation(source_image, source_semantics, target_semantics,
                                        self.generator, self.kp_extractor, self.he_estimator, self.mapping, 
                                        yaw_c_seq, pitch_c_seq, roll_c_seq, use_exp=True)

        predictions_video = predictions_video.reshape((-1,)+predictions_video.shape[2:])
        predictions_video = predictions_video[:frame_num]

        video = []
        for idx in range(predictions_video.shape[0]):
            image = predictions_video[idx]
            image = np.transpose(image.data.cpu().numpy(), [1, 2, 0]).astype(np.float32)
            video.append(image)
        result = img_as_ubyte(video)

        ### the generated video is 256x256, so we  keep the aspect ratio, 
        original_size = crop_info[0]
        if original_size:
            result = [cv2.resize(result_i,(256, int(256.0 * original_size[1]/original_size[0]) )) for result_i in result ]
        
        video_name = x['video_name'] + '.mp4'
        temp_video_file = os.path.join(video_save_dir, 'temp_'+video_name)
        
        imageio.mimsave(temp_video_file, result, fps=float(25))

        audio_path = x['audio_path']
        audio_name = os.path.splitext(os.path.split(audio_path)[-1])[0]
        new_audio_file = os.path.join(video_save_dir, audio_name+'.wav')
        start_time = 0
        # cog will not keep the .mp3 filename
        sound = AudioSegment.from_file(audio_path)
        frames = frame_num 
        end_time = start_time + frames*1/25*1000
        word1=sound.set_frame_rate(16000)
        word = word1[start_time:end_time]
        word.export(new_audio_file, format="wav")

        new_video_name = save_video_with_audio(temp_video_file, new_audio_file, video_save_dir)
        print(f'The generated video is named {new_video_name} in {video_save_dir}')

        # only add watermark to the full image.
        video_name_full = x['video_name'] + '_full.mp4'
        new_video_name = paste_pic(video_path=temp_video_file, pic_path=pic_path, crop_info=crop_info, new_audio_path=new_audio_file, video_save_dir=video_save_dir, preprocess=preprocess, pic_path_type=pic_path_type)
        print(f'The generated video is named {video_save_dir}/{video_name_full}')

        #### paste back then enhancers
        if enhancer:
            video_name_enhancer = x['video_name'] + '_enhanced.mp4'
            enhanced_video = os.path.join(video_save_dir, 'temp_'+video_name_enhancer)
            current_video = os.path.join(video_save_dir, new_video_name)
            enhanced_images = face_enhancer(current_video, method=enhancer, bg_upsampler=background_enhancer)

            imageio.mimsave(enhanced_video, enhanced_images, fps=float(25))
            
            new_video_name = save_video_with_audio(enhanced_video, new_audio_file, video_save_dir)
            print(f'The generated video is named {video_save_dir}/{video_name_enhancer}')
            os.remove(enhanced_video)

        os.remove(temp_video_file)
        os.remove(new_audio_file)

        return new_video_name

