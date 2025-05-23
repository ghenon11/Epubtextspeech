import re
import psutil

from StyleTTS2.Modules.diffusion.sampler import DiffusionSampler, ADPM2Sampler, KarrasSchedule
from StyleTTS2.Utils.PLBERT.util import load_plbert
from StyleTTS2.text_utils import TextCleaner

from StyleTTS2 import utils
from StyleTTS2 import models
import yaml
# from langchain.text_splitter import RecursiveCharacterTextSplitter
import numpy as np
import random
from cached_path import cached_path
import torchaudio
import torch
# Need install phonemizer modules and espeak https://github.com/espeak-ng/espeak-ng
# import phonemizer
# from phonemizer import phonemize
from phonemizer.backend import EspeakBackend
import scipy
import librosa
from pathlib import Path
import nltk
from nltk.tokenize import word_tokenize
import logging

import config

logger = logging.getLogger()

try:
    nltk.data.find('tokenizers/punkt')
except LookupError:
    nltk.download('punkt')

torch.manual_seed(0)
torch.backends.cudnn.benchmark = False
torch.backends.cudnn.deterministic = True

random.seed(0)

np.random.seed(0)

LIBRI_TTS_CHECKPOINT_URL = "https://huggingface.co/yl4579/StyleTTS2-LibriTTS/resolve/main/Models/LibriTTS/epochs_2nd_00020.pth"
LIBRI_TTS_CONFIG_URL = "https://huggingface.co/yl4579/StyleTTS2-LibriTTS/resolve/main/Models/LibriTTS/config.yml?download=true"

ASR_CHECKPOINT_URL = (
    "https://github.com/yl4579/StyleTTS2/raw/main/Utils/ASR/epoch_00080.pth"
)
ASR_CONFIG_URL = "https://github.com/yl4579/StyleTTS2/raw/main/Utils/ASR/config.yml"
F0_CHECKPOINT_URL = "https://github.com/yl4579/StyleTTS2/raw/main/Utils/JDC/bst.t7"
BERT_CHECKPOINT_URL = (
    "https://github.com/yl4579/StyleTTS2/raw/main/Utils/PLBERT/step_1000000.t7"
)
BERT_CONFIG_URL = "https://github.com/yl4579/StyleTTS2/raw/main/Utils/PLBERT/config.yml"

DEFAULT_TARGET_VOICE_URL = "https://styletts2.github.io/wavs/LJSpeech/OOD/GT/00001.wav"

SINGLE_INFERENCE_MAX_LEN = 420
INFERENCE_MIN_LEN = 5

to_mel = torchaudio.transforms.MelSpectrogram(
    n_mels=80, n_fft=2048, win_length=1200, hop_length=300
)
mean, std = -4, 4


def length_to_mask(lengths):
    mask = (
        torch.arange(lengths.max())
        .unsqueeze(0)
        .expand(lengths.shape[0], -1)
        .type_as(lengths)
    )
    mask = torch.gt(mask + 1, lengths.unsqueeze(1))
    return mask


def preprocess(wave):
    wave_tensor = torch.from_numpy(wave).float()
    mel_tensor = to_mel(wave_tensor)
    mel_tensor = (torch.log(1e-5 + mel_tensor.unsqueeze(0)) - mean) / std
    return mel_tensor


def unicode_code_points(s):
    # the unicode_code_points function takes a string s and returns a list of Unicode code points for each character in the string. The ord function is used to get the Unicode code point of each character, and f"U+{ord(char):04X}" formats it as a hexadecimal string prefixed with "U+".
    # Example usage
    # input_string = "Hello, World!"
    # unicode_codes = unicode_code_points(input_string)
    return [f"{char} U+{ord(char):04X}" for char in s]


def segment_text(text):
    # unicode_codes = unicode_code_points(text)
    # logging.debug(str(unicode_codes))

    # Step 0: Add " ." before newline if line doesn't end with punctuation
    text = re.sub(r'([^\.\?!;\s])?\n', lambda m: (
        m.group(1) + " ." if m.group(1) else ".") + "\n", text)

    # Step 1: Split by newline
    lines = text.splitlines()

    # Step 2: Split each line by primary delimiters (keeping delimiters)
    primary_delimiters = r'[.;?!]'
    segments = []
    for line in lines:
        parts = re.split(f'({primary_delimiters})', line)
        combined = [''.join(parts[i:i+2]).strip()
                    for i in range(0, len(parts)-1, 2)]
        if len(parts) % 2 != 0:
            combined.append(parts[-1].strip())
        segments.extend([s for s in combined if s])

    # Step 3: Merge short segments
    merged_segments = []
    buffer = ""

    for segment in segments:
        if len(buffer) > 0:
            buffer += " " + segment
        else:
            buffer = segment

        if len(buffer) >= INFERENCE_MIN_LEN:
            merged_segments.append(buffer.strip())
            buffer = ""

    if buffer:
        merged_segments.append(buffer.strip())

    # Step 4: Further split long segments
    def split_long_segment(segment):
        if len(segment) <= SINGLE_INFERENCE_MAX_LEN:
            return [segment.strip()]

        # First split by commas (keeping commas)
        comma_parts = re.split(r'(,)', segment)
        combined_comma = [''.join(comma_parts[i:i+2]).strip()
                          for i in range(0, len(comma_parts)-1, 2)]
        if len(comma_parts) % 2 != 0:
            combined_comma.append(comma_parts[-1].strip())

        final_chunks = []
        temp = ""
        for part in combined_comma:
            if len(temp) + len(part) + 1 <= SINGLE_INFERENCE_MAX_LEN:
                temp += " " + part if temp else part
            else:
                if temp:
                    final_chunks.append(temp.strip())
                temp = part
        if temp:
            final_chunks.append(temp.strip())

        # Then split any still-too-long parts by space
        result = []
        for chunk in final_chunks:
            if len(chunk) <= SINGLE_INFERENCE_MAX_LEN:
                result.append(chunk)
            else:
                words = chunk.split()
                temp = ""
                for word in words:
                    if len(temp) + len(word) + 1 <= SINGLE_INFERENCE_MAX_LEN:
                        temp += " " + word if temp else word
                    else:
                        result.append(temp.strip())
                        temp = word
                if temp:
                    result.append(temp.strip())

        return result

    # Apply long-segment splitting
    final_segments = []
    for segment in merged_segments:
        final_segments.extend(split_long_segment(segment))

    return final_segments


class StyleTTS2:
    def __init__(self, model_checkpoint_path=None, config_path=None, phoneme_converter="gruut"
                 ):
        import logging

        logger = logging.getLogger()
        logger.info("StylessTTS2 initialising")
        self.model = None
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        # GHE update don't use gruut
        # self.phoneme_converter = PhonemeConverterFactory.load_phoneme_converter(
        #    phoneme_converter
        # )
        # EspeakBackend.command = 'espeak'
        # GHE
        # was self.phoneme_converter = EspeakBackend(
        #    language='fr-fr', preserve_punctuation=True, with_stress=True, words_mismatch='ignore')
        self.phoneme_converter = EspeakBackend(
            language='fr-fr',
            preserve_punctuation=True,  # was True
            with_stress=True,
            words_mismatch='ignore'
        )
        logger.debug(f"Phoneme_converter is {self.phoneme_converter}")
        logger.debug(f"device is {self.device}")

        self.config = None
        self.model_params = None
        self.model = self.load_model(
            model_path=model_checkpoint_path, config_path=config_path
        )

        self.sampler = DiffusionSampler(
            self.model.diffusion.diffusion,
            sampler=ADPM2Sampler(),
            sigma_schedule=KarrasSchedule(
                sigma_min=0.0001, sigma_max=3.0, rho=9.0
            ),  # empirical parameters
            clamp=False,
        )

    def load_model(self, model_path=None, config_path=None):
        """
        Loads model to prepare for inference. Loads checkpoints from provided paths or from local cache (or downloads
        default checkpoints to local cache if not present).
        :param model_path: Path to LibriTTS StyleTTS2 model checkpoint (TODO: LJSpeech model support)
        :param config_path: Path to LibriTTS StyleTTS2 model config JSON (TODO: LJSpeech model support)
        :return:
        """
        logger.info("Loading StyleTTS model")
        if not model_path or not Path(model_path).exists():
            logger.warning(
                f"Invalid or missing model checkpoint path {model_path}. Loading default model..."
            )
            model_path = cached_path(LIBRI_TTS_CHECKPOINT_URL)

        if not config_path or not Path(config_path).exists():
            logger.warning(
                f"Invalid or missing config path {config_path}. Loading default config..."
            )
            config_path = cached_path(LIBRI_TTS_CONFIG_URL)

        self.config = yaml.safe_load(open(config_path))

        # load pretrained ASR model
        ASR_config = self.config.get("ASR_config", False)
        if not ASR_config or not Path(ASR_config).exists():
            logger.warning(
                f"Invalid ASR config path {ASR_config}. Loading default config..."
            )
            ASR_config = cached_path(ASR_CONFIG_URL)
        ASR_path = self.config.get("ASR_path", False)
        if not ASR_path or not Path(ASR_path).exists():
            logger.warning(
                f"Invalid ASR model checkpoint path {ASR_path}. Loading default model..."
            )
            ASR_path = cached_path(ASR_CHECKPOINT_URL)
        text_aligner = models.load_ASR_models(ASR_path, ASR_config)

        # load pretrained F0 model
        F0_path = self.config.get("F0_path", False)
        if not F0_path or not Path(F0_path).exists():
            logger.warning(
                f"Invalid F0 model path {F0_path}. Loading default model...")
            F0_path = cached_path(F0_CHECKPOINT_URL)
        pitch_extractor = models.load_F0_models(F0_path)

        # load BERT model
        BERT_dir_path = self.config.get(
            "PLBERT_dir", False
        )  # Directory at BERT_dir_path should contain PLBERT config.yml AND checkpoint
        if not BERT_dir_path or not Path(BERT_dir_path).exists():
            logger.warning(
                f"Invalid or missing PLBERT path {BERT_dir_path}. Loading default config..."
            )
            BERT_config_path = cached_path(BERT_CONFIG_URL)
            BERT_checkpoint_path = cached_path(BERT_CHECKPOINT_URL)
            plbert = load_plbert(
                None, config_path=BERT_config_path, checkpoint_path=BERT_checkpoint_path
            )
        else:
            # GHE Added
            # BERT_dir_path = Path(BERT_dir_path)
            plbert = load_plbert(BERT_dir_path)

        self.model_params = utils.recursive_munch(self.config["model_params"])
        model = models.build_model(
            self.model_params, text_aligner, pitch_extractor, plbert)
        _ = [model[key].eval() for key in model]
        _ = [model[key].to(self.device) for key in model]

        params_whole = torch.load(
            model_path, map_location="cpu")
        params = params_whole["net"]

        for key in model:
            if key in params:
                logger.debug("key %s loaded" % key)
                try:
                    model[key].load_state_dict(params[key])
                except Exception as e:
                    logging.debug(
                        f"Exception {str(e)} when loading state dict"
                    )
                    from collections import OrderedDict

                    state_dict = params[key]
                    new_state_dict = OrderedDict()
                    for k, v in state_dict.items():
                        name = k[7:]  # remove `module.`
                        new_state_dict[name] = v
                    # load params
                    model[key].load_state_dict(new_state_dict, strict=False)
        #             except:
        #                 _load(params[key], model[key])
        _ = [model[key].eval() for key in model]

        return model

    def compute_style(self, path):
        """
        Compute style vector, essentially an embedding that captures the characteristics
        of the target voice that is being cloned
        :param path: Path to target voice audio file
        :return: style vector
        """
        wave, sr = librosa.load(path, sr=24000)
        audio, index = librosa.effects.trim(wave, top_db=30)
        if sr != 24000:
            audio = librosa.resample(audio, sr, 24000)
        mel_tensor = preprocess(audio).to(self.device)

        with torch.no_grad():
            ref_s = self.model.style_encoder(mel_tensor.unsqueeze(1))
            ref_p = self.model.predictor_encoder(mel_tensor.unsqueeze(1))

        return torch.cat([ref_s, ref_p], dim=1)

    def inference(
        self,
        text: str,
        target_voice_path=None,
        output_wav_file=None,
        output_sample_rate=24000,
        alpha=0.3,
        beta=0.7,
        diffusion_steps=5,
        embedding_scale=1,
        ref_s=None,
    ):
        """
        Text-to-speech function
        :param text: Input text to turn into speech.
        :param target_voice_path: Path to audio file of target voice to clone.
        :param output_wav_file: Name of output audio file (if output WAV file is desired).
        :param output_sample_rate: Output sample rate (default 24000).
        :param alpha: Determines timbre of speech, higher means style is more suitable to text than to the target voice.
        :param beta: Determines prosody of speech, higher means style is more suitable to text than to the target voice.
        :param diffusion_steps: The more the steps, the more diverse the samples are, with the cost of speed.
        :param embedding_scale: Higher scale means style is more conditional to the input text and hence more emotional.
        :param ref_s: Pre-computed style vector to pass directly.
        :return: audio data as a Numpy array (will also create the WAV file if output_wav_file was set).
        """

        # BERT model is limited by a tensor size [1, 512] during its inference, which roughly corresponds to ~450 characters
        if len(text) > SINGLE_INFERENCE_MAX_LEN:
            logger.info(
                f"Text is longer than SINGLE_INFERENCE_MAX_LEN {SINGLE_INFERENCE_MAX_LEN}")
            return self.long_inference(
                text,
                target_voice_path=target_voice_path,
                output_wav_file=output_wav_file,
                output_sample_rate=output_sample_rate,
                alpha=alpha,
                beta=beta,
                diffusion_steps=diffusion_steps,
                embedding_scale=embedding_scale,
                ref_s=ref_s,
            )

        if ref_s is None:
            # default to clone https://styletts2.github.io/wavs/LJSpeech/OOD/GT/00001.wav voice from LibriVox (public domain)
            if not target_voice_path or not Path(target_voice_path).exists():
                logger.info("Cloning default target voice...")
                target_voice_path = cached_path(DEFAULT_TARGET_VOICE_URL)
            ref_s = self.compute_style(
                target_voice_path)  # target style vector

        logger.info(
            f"Synthetizing text starting with [{text[:15]}], ending with [{text[-15:]}]")
        text = text.strip()
        text = text.replace('"', "")
        # GHE update
        # Input text to phonemize() is str but it must be list of str
        phonemized_text = '\n'.join(
            self.phoneme_converter.phonemize(text.splitlines()))
        # original; phonemized_text = self.phoneme_converter.phonemize(str(text))
        ps = word_tokenize(phonemized_text)
        phoneme_string = " ".join(ps)

        textcleaner = TextCleaner()
        tokens = textcleaner(phoneme_string)
        tokens.insert(0, 0)
        tokens = torch.LongTensor(tokens).to(self.device).unsqueeze(0)

        with torch.no_grad():
            input_lengths = torch.LongTensor(
                [tokens.shape[-1]]).to(self.device)
            text_mask = length_to_mask(input_lengths).to(self.device)

            t_en = self.model.text_encoder(tokens, input_lengths, text_mask)
            bert_dur = self.model.bert(
                tokens, attention_mask=(~text_mask).int())
            d_en = self.model.bert_encoder(bert_dur).transpose(-1, -2)

            s_pred = self.sampler(
                noise=torch.randn((1, 256)).unsqueeze(1).to(self.device),
                embedding=bert_dur,
                embedding_scale=embedding_scale,
                features=ref_s,  # reference from the same speaker as the embedding
                num_steps=diffusion_steps,
            ).squeeze(1)

            s = s_pred[:, 128:]
            ref = s_pred[:, :128]

            ref = alpha * ref + (1 - alpha) * ref_s[:, :128]
            s = beta * s + (1 - beta) * ref_s[:, 128:]

            # duration prediction
            d = self.model.predictor.text_encoder(
                d_en, s, input_lengths, text_mask)

            x, _ = self.model.predictor.lstm(d)
            duration = self.model.predictor.duration_proj(x)

            duration = torch.sigmoid(duration).sum(axis=-1)
            pred_dur = torch.round(duration.squeeze()).clamp(min=1)

            pred_aln_trg = torch.zeros(input_lengths, int(pred_dur.sum().data))
            c_frame = 0
            for i in range(pred_aln_trg.size(0)):
                pred_aln_trg[i, c_frame: c_frame + int(pred_dur[i].data)] = 1
                c_frame += int(pred_dur[i].data)

            # encode prosody
            en = d.transpose(-1, -
                             2) @ pred_aln_trg.unsqueeze(0).to(self.device)
            if self.model_params.decoder.type == "hifigan":
                asr_new = torch.zeros_like(en)
                asr_new[:, :, 0] = en[:, :, 0]
                asr_new[:, :, 1:] = en[:, :, 0:-1]
                en = asr_new

            F0_pred, N_pred = self.model.predictor.F0Ntrain(en, s)

            asr = t_en @ pred_aln_trg.unsqueeze(0).to(self.device)
            if self.model_params.decoder.type == "hifigan":
                asr_new = torch.zeros_like(asr)
                asr_new[:, :, 0] = asr[:, :, 0]
                asr_new[:, :, 1:] = asr[:, :, 0:-1]
                asr = asr_new

            out = self.model.decoder(asr, F0_pred, N_pred,
                                     ref.squeeze().unsqueeze(0))

        # GHE trim 100 to be consistent, was 50
        output = (
            out.squeeze().cpu().numpy()[..., :-100]
        )  # weird pulse at the end of the model, need to be fixed later
        if output_wav_file:
            scipy.io.wavfile.write(
                output_wav_file, rate=output_sample_rate, data=output)
        return output

    def long_inference(
        self,
        text: str,
        target_voice_path=None,
        output_wav_file=None,
        output_sample_rate=24000,
        alpha=0.3,
        beta=0.7,
        t=0.7,
        diffusion_steps=5,
        embedding_scale=1,
        ref_s=None,
    ):
        """
        Inference for longform text. Used automatically in inference() when needed.
        :param text: Input text to turn into speech.
        :param target_voice_path: Path to audio file of target voice to clone.
        :param output_wav_file: Name of output audio file (if output WAV file is desired).
        :param output_sample_rate: Output sample rate (default 24000).
        :param alpha: Determines timbre of speech, higher means style is more suitable to text than to the target voice.
        :param beta: Determines prosody of speech, higher means style is more suitable to text than to the target voice.
        :param t: Determines consistency of style across inference segments (0 lowest, 1 highest)
        :param diffusion_steps: The more the steps, the more diverse the samples are, with the cost of speed.
        :param embedding_scale: Higher scale means style is more conditional to the input text and hence more emotional.
        :param ref_s: Pre-computed style vector to pass directly.
        :return: concatenated audio data as a Numpy array (will also create the WAV file if output_wav_file was set).
        """

        if ref_s is None:
            # default to clone https://styletts2.github.io/wavs/LJSpeech/OOD/GT/00001.wav voice from LibriVox (public domain)
            if not target_voice_path or not Path(target_voice_path).exists():
                logger.info("Cloning default target voice...")
                target_voice_path = cached_path(DEFAULT_TARGET_VOICE_URL)
            ref_s = self.compute_style(
                target_voice_path)  # target style vector
        logger.info("Segmenting text")
        text_segments = segment_text(text)
        nb_segments = len(text_segments)
        logger.info(f"Text segmented in {nb_segments} segments")
        segments = []
        prev_s = None
        i = 1
        for text_segment in text_segments:
            # GHE update
            # Address cut-off sentence issue due to langchain text splitter
            # if text_segment[-1] != ".":
            #     logger.warning(
            #         f"cut-off sentence issue due to langchain text splitter, last 10 characters: {text_segment[-10:]}")
            #     text_segment += ", "
            logger.info(f"Synthetizing segment {i} of {nb_segments}")

            segment_output, prev_s = self.long_inference_segment(
                text_segment,
                prev_s,
                ref_s,
                alpha=alpha,
                beta=beta,
                t=t,
                diffusion_steps=diffusion_steps,
                embedding_scale=embedding_scale,
            )
            segments.append(segment_output)
            i += 1
        output = np.concatenate(segments)
        if output_wav_file:
            scipy.io.wavfile.write(
                output_wav_file, rate=output_sample_rate, data=output)
        return output

    # GHE
    # t was 0.7, trying 0.9 no change
    # diffusion step was 5 try 4 no change
    def long_inference_segment(
        self,
        text,
        prev_s,
        ref_s,
        alpha=0.3,
        beta=0.7,
        t=0.7,
        diffusion_steps=5,
        embedding_scale=1,
    ):
        """
        Performs inference for segment of longform text; see long_inference()
        :param text: Input text
        :param prev_s: Style vector of previous speech segment (used to keep voice consistent in longform inference)
        :param ref_s: Pre-computed style vector of target voice to clone
        :param alpha: Determines timbre of speech, higher means style is more suitable to text than to the target voice.
        :param beta: Determines prosody of speech, higher means style is more suitable to text than to the target voice.
        :param t: Determines consistency of style across inference segments (0 lowest, 1 highest)
        :param diffusion_steps: The more the steps, the more diverse the samples are, with the cost of speed.
        :param embedding_scale: Higher scale means style is more conditional to the input text and hence more emotional.
        :return: audio data as a Numpy array
        """
        text = text.strip()
        text = text.replace('"', "")
        # GHE updateSynthetizing
        # phonemized_text = self.phoneme_converter.phonemize(text)
        logger.info(
            f"Synthetizing text segment starting with [{text[:50]}], ending with [{text[-50:]}]")
        # unicode_codes = unicode_code_points(text)
        # logging.debug(str(unicode_codes))
        # GHE, updated phonemize
        # was phonemized_text = '\n'.join(
        #    self.phoneme_converter.phonemize(text.splitlines()))

        lines = text.splitlines()
        try:
            phonemized_lines = self.phoneme_converter.phonemize(lines)
        except Exception as e:
            logger.warning(f"[Phonemizer] failed: {e}")
            phonemized_lines = self.phoneme_converter.phonemize(
                [' '.join(lines)])

        if len(phonemized_lines) != len(lines):
            logger.warning(
                f"[Phonemizer] Line mismatch: {len(lines)} input vs {len(phonemized_lines)} output")
            logger.warning(f"[Phonemizer] Input lines: {lines}")
            logger.warning(f"[Phonemizer] Output lines: {phonemized_lines}")
            phonemized_text = self.phoneme_converter.phonemize([' '.join(lines)])[
                0]
        else:
            phonemized_text = '\n'.join(phonemized_lines)

        # 🔠 Bonus Debug: Word count alignment logging
        original_word_count = len(word_tokenize(text))
        phoneme_word_count = len(word_tokenize(phonemized_text))
        logger.debug(
            f"[Phonemizer] Original words: {original_word_count}, Phonemized tokens: {phoneme_word_count}")

        # ❗ Optional safety assertion — uncomment if you want hard stop on large mismatches
        # assert abs(original_word_count - phoneme_word_count) <= original_word_count * 0.3, \
        #     f"[Phonemizer] Token mismatch too large! ({original_word_count} vs {phoneme_word_count})"

        ps = word_tokenize(str(phonemized_text))
        phoneme_string = " ".join(ps)
        phoneme_string = phoneme_string.replace("``", '"')
        phoneme_string = phoneme_string.replace("''", '"')

        textcleaner = TextCleaner()
        tokens = textcleaner(phoneme_string)
        tokens.insert(0, 0)
        tokens = torch.LongTensor(tokens).to(self.device).unsqueeze(0)

        attempts = 0
        MAX_SYNTH=int(config.MAX_SYNTH)
        while attempts < int(config.MAX_SYNTH_ATTEMPTS):
            with torch.no_grad():
                input_lengths = torch.LongTensor(
                    [tokens.shape[-1]]).to(self.device)
                text_mask = length_to_mask(input_lengths).to(self.device)

                t_en = self.model.text_encoder(
                    tokens, input_lengths, text_mask)
                bert_dur = self.model.bert(
                    tokens, attention_mask=(~text_mask).int())
                d_en = self.model.bert_encoder(bert_dur).transpose(-1, -2)

                s_pred = self.sampler(
                    noise=torch.randn((1, 256)).unsqueeze(1).to(self.device),
                    embedding=bert_dur,
                    embedding_scale=embedding_scale,
                    features=ref_s,  # reference from the same speaker as the embedding
                    num_steps=diffusion_steps,
                ).squeeze(1)

                if prev_s is not None:
                    # convex combination of previous and current style
                    s_pred = t * prev_s + (1 - t) * s_pred

                s = s_pred[:, 128:]
                ref = s_pred[:, :128]

                ref = alpha * ref + (1 - alpha) * ref_s[:, :128]
                s = beta * s + (1 - beta) * ref_s[:, 128:]

                s_pred = torch.cat([ref, s], dim=-1)

                d = self.model.predictor.text_encoder(
                    d_en, s, input_lengths, text_mask)

                x, _ = self.model.predictor.lstm(d)
                duration = self.model.predictor.duration_proj(x)

                duration = torch.sigmoid(duration).sum(axis=-1)
                pred_dur = torch.round(duration.squeeze()).clamp(min=1)

                pred_aln_trg = torch.zeros(
                    input_lengths, int(pred_dur.sum().data))
                c_frame = 0
                for i in range(pred_aln_trg.size(0)):
                    pred_aln_trg[i, c_frame: c_frame +
                                 int(pred_dur[i].data)] = 1
                    c_frame += int(pred_dur[i].data)

                # encode prosody
                en = d.transpose(-1, -
                                 2) @ pred_aln_trg.unsqueeze(0).to(self.device)
                if self.model_params.decoder.type == "hifigan":
                    asr_new = torch.zeros_like(en)
                    asr_new[:, :, 0] = en[:, :, 0]
                    asr_new[:, :, 1:] = en[:, :, 0:-1]
                    en = asr_new

                F0_pred, N_pred = self.model.predictor.F0Ntrain(en, s)

                asr = t_en @ pred_aln_trg.unsqueeze(0).to(self.device)
                if self.model_params.decoder.type == "hifigan":
                    asr_new = torch.zeros_like(asr)
                    asr_new[:, :, 0] = asr[:, :, 0]
                    asr_new[:, :, 1:] = asr[:, :, 0:-1]
                    asr = asr_new

                out = self.model.decoder(asr, F0_pred, N_pred,
                                         ref.squeeze().unsqueeze(0))
                output = out.squeeze().cpu().numpy()[..., :-50]

                try:
                    with np.errstate(over='raise', invalid='raise'):
                       # synth_mean = np.mean(np.abs(output)) * 100
                        max_value = np.max(np.abs(output)) * 100
                except FloatingPointError:
                    logger.error(
                        "Overflow or invalid value detected during synth_mean calculation.")
                    max_value = MAX_SYNTH  # to force retry

                if np.isnan(max_value):
                    logger.error(
                        f"Segment produced NaN, memory usage is {psutil.virtual_memory().percent}%")
                    max_value = MAX_SYNTH # to force retry
                else:
                    logger.info(
                        f"Segment max value is {max_value:.2f}, memory usage is {psutil.virtual_memory().percent}% for attempts {attempts+1}")

                if max_value <= MAX_SYNTH:
                    return output, s_pred
                logger.warning(
                    f"Segment max value is too high (above {MAX_SYNTH}) or segment failed, retrying segment inference...")
                attempts += 1
        return output, s_pred
