from utils.plugin_loader import PluginLoader
from config import config as cfg
import torch
import os
import numpy as np
from test_tools.common import detect_all, grab_all_frames
from test_tools.utils import get_crop_box
from test_tools.ct.operations import find_longest, multiple_tracking
from test_tools.faster_crop_align_xray import FasterCropAlignXRay
from test_tools.supply_writer import SupplyWriter
import argparse
from tqdm import tqdm

mean = torch.tensor([0.485 * 255, 0.456 * 255, 0.406 * 255,]).cuda().view(1, 3, 1, 1, 1)
std = torch.tensor([0.229 * 255, 0.224 * 255, 0.225 * 255,]).cuda().view(1, 3, 1, 1, 1)

def create_output_video(video_path, frames, frame_res, output_dir="./output"):
    os.makedirs(output_dir, exist_ok=True)
    basename = os.path.splitext(os.path.basename(video_path))[0] + ".avi"
    out_file = os.path.join(output_dir, basename)

    boxes = []
    scores = []

    for frame_idx in range(len(frames)):
        if frame_idx in frame_res:
            pred_prob = np.mean(frame_res[frame_idx])
            rect = frame_boxes[frame_idx]
        else:
            pred_prob = None
            rect = None
        scores.append(pred_prob)
        boxes.append(rect)

    SupplyWriter(args.video, out_file, 0.002584857167676091).run(frames, scores, boxes)

def predict_deepfake_video(video_path, checkpoint_path="./checkpoints/ftcn_tt.pth"):
    cfg.init_with_yaml()
    cfg.update_with_yaml("ftcn_tt.yaml")
    cfg.freeze()

    classifier = PluginLoader.get_classifier(cfg.classifier_type)()
    classifier.cuda()
    classifier.eval()
    classifier.load(checkpoint_path)

    crop_align_func = FasterCropAlignXRay(cfg.imsize)

    max_frame = 300
    
    detect_res, all_lm68, frames = detect_all(
        video_path, return_frames=True, max_size=max_frame
    )

    shape = frames[0].shape[:2]
    all_detect_res = []

    assert len(all_lm68) == len(detect_res)

    for faces, faces_lm68 in zip(detect_res, all_lm68):
        new_faces = []
        for (box, lm5, score), face_lm68 in zip(faces, faces_lm68):
            new_face = (box, lm5, face_lm68, score)
            new_faces.append(new_face)
        all_detect_res.append(new_faces)

    detect_res = all_detect_res

    tracks = multiple_tracking(detect_res)
    tuples = [(0, len(detect_res))] * len(tracks)

    if len(tracks) == 0:
        tuples, tracks = find_longest(detect_res)
    
    data_storage = {}
    frame_boxes = {}
    super_clips = []

    for track_i, ((start, end), track) in enumerate(zip(tuples, tracks)):
        assert len(detect_res[start:end]) == len(track)

        super_clips.append(len(track))

        for face, frame_idx, j in zip(track, range(start, end), range(len(track))):
            box,lm5,lm68 = face[:3]
            big_box = get_crop_box(shape, box, scale=0.5)

            top_left = big_box[:2][None, :]

            new_lm5 = lm5 - top_left
            new_lm68 = lm68 - top_left

            new_box = (box.reshape(2, 2) - top_left).reshape(-1)

            info = (new_box, new_lm5, new_lm68, big_box)


            x1, y1, x2, y2 = big_box
            cropped = frames[frame_idx][y1:y2, x1:x2]

            base_key = f"{track_i}_{j}_"
            data_storage[base_key + "img"] = cropped
            data_storage[base_key + "ldm"] = info
            data_storage[base_key + "idx"] = frame_idx

            frame_boxes[frame_idx] = np.rint(box).astype(np.int32)

    clips_for_video = []
    clip_size = cfg.clip_size
    pad_length = clip_size - 1

    for super_clip_idx, super_clip_size in enumerate(super_clips):
        inner_index = list(range(super_clip_size))
        if super_clip_size < clip_size: # padding
            post_module = inner_index[1:-1][::-1] + inner_index

            l_post = len(post_module)
            post_module = post_module * (pad_length // l_post + 1)
            post_module = post_module[:pad_length]
            assert len(post_module) == pad_length

            pre_module = inner_index + inner_index[1:-1][::-1]
            l_pre = len(post_module)
            pre_module = pre_module * (pad_length // l_pre + 1)
            pre_module = pre_module[-pad_length:]
            assert len(pre_module) == pad_length

            inner_index = pre_module + inner_index + post_module

        super_clip_size = len(inner_index)

        frame_range = [
            inner_index[i : i + clip_size] for i in range(super_clip_size) if i + clip_size <= super_clip_size
        ]
        for indices in frame_range:
            clip = [(super_clip_idx, t) for t in indices]
            clips_for_video.append(clip)

    preds = []
    frame_res = {}

    for clip in clips_for_video, desc="testing":
        images = [data_storage[f"{i}_{j}_img"] for i, j in clip]
        landmarks = [data_storage[f"{i}_{j}_ldm"] for i, j in clip]
        frame_ids = [data_storage[f"{i}_{j}_idx"] for i, j in clip]
        landmarks, images = crop_align_func(landmarks, images)
        images = torch.as_tensor(images, dtype=torch.float32).cuda().permute(3, 0, 1, 2)
        images = images.unsqueeze(0).sub(mean).div(std)

        with torch.no_grad():
            output = classifier(images)

        pred = float(output["final_output"])
        for f_id in frame_ids:
            if f_id not in frame_res:
                frame_res[f_id] = []
            frame_res[f_id].append(pred)
        preds.append(pred)

    video_score = np.mean(preds)
    print(f"{video_path} Score: {video_score}")

    return video_score, frames, frame_res


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=str, help="input video")
    parser.add_argument("--out_dir", type=str, help="Directory for output", required=False)
    parser.add_argument("--checkpoint_path", type=str, default="./checkpoints/ftcn_tt.pth", help="Path to the checkpoint file", required=False)

    args = parser.parse_args()
    
    video_score, frames, frame_res = predict_deepfake_video(args.video, args.checkpoint_path)
    if args.out_dir:
        create_output_video(args.video, frames, frame_res, args.out_dir)
