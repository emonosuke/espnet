from contextlib import contextmanager
from distutils.version import LooseVersion
import logging
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple
from typing import Union

import torch
from typeguard import check_argument_types

from espnet.nets.e2e_asr_common import ErrorCalculator as ASRErrorCalculator
from espnet.nets.e2e_mt_common import ErrorCalculator as MTErrorCalculator
from espnet.nets.pytorch_backend.nets_utils import th_accuracy
from espnet.nets.pytorch_backend.transformer.add_sos_eos import add_sos_eos
from espnet.nets.pytorch_backend.transformer.label_smoothing_loss import (
    LabelSmoothingLoss,  # noqa: H301
)
from espnet2.asr.ctc import CTC
from espnet2.asr.decoder.abs_decoder import AbsDecoder
from espnet2.asr.encoder.abs_encoder import AbsEncoder
from espnet2.asr.frontend.abs_frontend import AbsFrontend
from espnet2.asr.postencoder.abs_postencoder import AbsPostEncoder
from espnet2.asr.preencoder.abs_preencoder import AbsPreEncoder
from espnet2.asr.specaug.abs_specaug import AbsSpecAug
from espnet2.layers.abs_normalize import AbsNormalize
from espnet2.torch_utils.device_funcs import force_gatherable
from espnet2.train.abs_espnet_model import AbsESPnetModel

import random
from itertools import groupby
from torch.nn.utils.rnn import pad_sequence

if LooseVersion(torch.__version__) >= LooseVersion("1.6.0"):
    from torch.cuda.amp import autocast
else:
    # Nothing to do if torch<1.6.0
    @contextmanager
    def autocast(enabled=True):
        yield


class ESPnetSTMDSampModel(AbsESPnetModel):
    """CTC-attention hybrid Encoder-Decoder model"""

    def __init__(
        self,
        vocab_size: int,
        token_list: Union[Tuple[str, ...], List[str]],
        frontend: Optional[AbsFrontend],
        specaug: Optional[AbsSpecAug],
        normalize: Optional[AbsNormalize],
        preencoder: Optional[AbsPreEncoder],
        encoder: AbsEncoder,
        encoder_mt: AbsEncoder,
        postencoder: Optional[AbsPostEncoder],
        decoder: AbsDecoder,
        asr_decoder: AbsDecoder,
        ctc: CTC,
        src_vocab_size: int = 0,
        src_token_list: Union[Tuple[str, ...], List[str]] = [],
        asr_weight: float = 0.0,
        mt_weight: float = 0.0,
        mtlalpha: float = 0.0,
        ctc_sample_rate: float = 0.0,
        ignore_id: int = -1,
        lsm_weight: float = 0.0,
        length_normalized_loss: bool = False,
        report_cer: bool = True,
        report_wer: bool = True,
        report_bleu: bool = True,
        sym_space: str = "<space>",
        sym_blank: str = "<blank>",
        extract_feats_in_collect_stats: bool = True,
        speech_attn: bool = False,
    ):
        assert check_argument_types()
        assert 0.0 < asr_weight < 1.0, "asr_weight should be (0.0, 1.0)"
        assert 0.0 <= mt_weight < 1.0, "mt_weight should be [0.0, 1.0)"
        assert 0.0 <= mtlalpha < 1.0, "mtlalpha should be [0.0, 1.0)"

        super().__init__()
        # note that eos is the same as sos (equivalent ID)
        self.sos = vocab_size - 1
        self.eos = vocab_size - 1
        self.src_sos = src_vocab_size - 1
        self.src_eos = src_vocab_size - 1
        self.vocab_size = vocab_size
        self.src_vocab_size = src_vocab_size
        self.ignore_id = ignore_id
        self.asr_weight = asr_weight
        self.mt_weight = mt_weight
        self.mtlalpha = mtlalpha
        self.ctc_sample_rate = ctc_sample_rate
        self.token_list = token_list.copy()
        self.src_token_list = src_token_list.copy()
        self.speech_attn = speech_attn

        self.frontend = frontend
        self.specaug = specaug
        self.normalize = normalize
        self.preencoder = preencoder
        self.postencoder = postencoder
        self.encoder = encoder
        self.encoder_mt = encoder_mt
        self.decoder = decoder

        self.criterion_st = LabelSmoothingLoss(
            size=vocab_size,
            padding_idx=ignore_id,
            smoothing=lsm_weight,
            normalize_length=length_normalized_loss,
        )

        self.criterion_asr = LabelSmoothingLoss(
            size=src_vocab_size,
            padding_idx=ignore_id,
            smoothing=lsm_weight,
            normalize_length=length_normalized_loss,
        )

        # submodule for ASR task
        assert (asr_decoder is not None), "ASR decoder needs to be present for MD"
        assert (
            src_token_list is not None
        ), "Missing src_token_list, cannot add asr module to st model"
        if self.mtlalpha > 0.0:
            self.ctc = ctc
        self.asr_decoder = asr_decoder

        # MT error calculator
        if report_bleu:
            self.mt_error_calculator = MTErrorCalculator(
                token_list, sym_space, sym_blank, report_bleu
            )
        else:
            self.mt_error_calculator = None

        # ASR error calculator
        if report_cer or report_wer:
            assert (
                src_token_list is not None
            ), "Missing src_token_list, cannot add asr module to st model"
            self.asr_error_calculator = ASRErrorCalculator(
                src_token_list, sym_space, sym_blank, report_cer, report_wer
            )
        else:
            self.asr_error_calculator = None

        self.extract_feats_in_collect_stats = extract_feats_in_collect_stats

    def forward(
        self,
        speech: torch.Tensor,
        speech_lengths: torch.Tensor,
        text: torch.Tensor,
        text_lengths: torch.Tensor,
        src_text: Optional[torch.Tensor],
        src_text_lengths: Optional[torch.Tensor],
    ) -> Tuple[torch.Tensor, Dict[str, torch.Tensor], torch.Tensor]:
        """Frontend + Encoder + Decoder + Calc loss

        Args:
            speech: (Batch, Length, ...)
            speech_lengths: (Batch,)
            text: (Batch, Length)
            text_lengths: (Batch,)
            src_text: (Batch, length)
            src_text_lengths: (Batch,)
        """
        assert text_lengths.dim() == 1, text_lengths.shape
        # Check that batch_size is unified
        assert (
            speech.shape[0]
            == speech_lengths.shape[0]
            == text.shape[0]
            == text_lengths.shape[0]
        ), (speech.shape, speech_lengths.shape, text.shape, text_lengths.shape)

        # additional checks with valid src_text
        assert src_text is not None, "missing source text for asr sub-task of ST"
        assert src_text_lengths.dim() == 1, src_text_lengths.shape
        assert text.shape[0] == src_text.shape[0] == src_text_lengths.shape[0], (
            text.shape,
            src_text.shape,
            src_text_lengths.shape,
        )

        batch_size = speech.shape[0]

        # for data-parallel
        text = text[:, : text_lengths.max()]
        if src_text is not None:
            src_text = src_text[:, : src_text_lengths.max()]

        # 1. Encoder
        encoder_out, encoder_out_lens = self.encode(speech, speech_lengths)

        do_ctc_sample = random.uniform(0, 1) < self.ctc_sample_rate
        if self.training and do_ctc_sample:
            ys_hat = self.ctc.argmax(encoder_out).data
            ys_hat = [[x[0] for x in groupby(ys)] for ys in ys_hat]
            ys_hat = [[x for x in filter(lambda x: x != 0, ys)] for ys in ys_hat]
            for i, ys in enumerate(ys_hat):
                if len(ys) == 0:
                    ys_hat[i] = [x for x in src_text[i] if x != -1]
            ys_hat_lens = torch.tensor([len(x) for x in ys_hat], device=speech.device)
            ys_hat = [torch.tensor(ys, device=speech.device) for ys in ys_hat]
            ys_hat = pad_sequence(ys_hat, batch_first=True, padding_value=-1)

            # 2a. ASR Decoder
            (
                loss_asr_att,
                _,
                _,
                _,
                hs_dec_asr,
            ) = self._calc_asr_att_loss(encoder_out, encoder_out_lens, ys_hat, ys_hat_lens)
            acc_asr_att = None
            cer_asr_att = None
            wer_asr_att = None
        else:
            # 2a. ASR Decoder
            (
                loss_asr_att,
                acc_asr_att,
                cer_asr_att,
                wer_asr_att,
                hs_dec_asr,
            ) = self._calc_asr_att_loss(encoder_out, encoder_out_lens, src_text, src_text_lengths)

        # 2b. CTC branch
        if self.mtlalpha > 0:
            loss_asr_ctc, cer_asr_ctc = self._calc_ctc_loss(
                encoder_out, encoder_out_lens, src_text, src_text_lengths
            )
        else:
            loss_asr_ctc, cer_asr_ctc = 0, None

        # 3a. MT Encoder
        if self.training and do_ctc_sample:
            dec_asr_lengths = ys_hat_lens + 1
        else:
            dec_asr_lengths = src_text_lengths + 1
        encoder_mt_out, encoder_mt_out_lens, _ = self.encoder_mt(hs_dec_asr, dec_asr_lengths)

        if self.speech_attn:
            speech_out = encoder_out
            speech_lens = encoder_out_lens
        # 2a. Attention-decoder branch (ST)
        loss_st_att, acc_st_att, bleu_st_att = self._calc_mt_att_loss(
            encoder_mt_out, encoder_mt_out_lens, text, text_lengths, speech_out, speech_lens
        )

        # 3. Loss computation
        if self.training and do_ctc_sample:
            asr_ctc_weight = 1.0
        else:
            asr_ctc_weight = self.mtlalpha
        loss_st = loss_st_att
        if asr_ctc_weight == 0.0:
            loss_asr = loss_asr_att
        else:
            loss_asr = (
                asr_ctc_weight * loss_asr_ctc + (1 - asr_ctc_weight) * loss_asr_att
            )
        loss = (
            (1 - self.asr_weight) * loss_st
            + self.asr_weight * loss_asr
        )

        stats = dict(
            loss=loss.detach(),
            loss_asr=loss_asr.detach() if type(loss_asr) is not float else loss_asr,
            loss_st=loss_st.detach() if type(loss_asr) is not float else loss_asr,
            acc_asr=acc_asr_att,
            acc=acc_st_att,
            cer_ctc=cer_asr_ctc,
            cer=cer_asr_att,
            wer=wer_asr_att,
            bleu=bleu_st_att,
        )
        
        # force_gatherable: to-device and to-tensor if scalar for DataParallel
        loss, stats, weight = force_gatherable((loss, stats, batch_size), loss.device)
        return loss, stats, weight

    def collect_feats(
        self,
        speech: torch.Tensor,
        speech_lengths: torch.Tensor,
        text: torch.Tensor,
        text_lengths: torch.Tensor,
        src_text: Optional[torch.Tensor],
        src_text_lengths: Optional[torch.Tensor],
    ) -> Dict[str, torch.Tensor]:
        if self.extract_feats_in_collect_stats:
            feats, feats_lengths = self._extract_feats(speech, speech_lengths)
        else:
            # Generate dummy stats if extract_feats_in_collect_stats is False
            logging.warning(
                "Generating dummy stats for feats and feats_lengths, "
                "because encoder_conf.extract_feats_in_collect_stats is "
                f"{self.extract_feats_in_collect_stats}"
            )
            feats, feats_lengths = speech, speech_lengths
        return {"feats": feats, "feats_lengths": feats_lengths}

    def encode(
        self, speech: torch.Tensor, speech_lengths: torch.Tensor
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Frontend + Encoder. Note that this method is used by st_inference.py

        Args:
            speech: (Batch, Length, ...)
            speech_lengths: (Batch, )
        """
        with autocast(False):
            # 1. Extract feats
            feats, feats_lengths = self._extract_feats(speech, speech_lengths)

            # 2. Data augmentation
            if self.specaug is not None and self.training:
                feats, feats_lengths = self.specaug(feats, feats_lengths)

            # 3. Normalization for feature: e.g. Global-CMVN, Utterance-CMVN
            if self.normalize is not None:
                feats, feats_lengths = self.normalize(feats, feats_lengths)

        # Pre-encoder, e.g. used for raw input data
        if self.preencoder is not None:
            feats, feats_lengths = self.preencoder(feats, feats_lengths)

        # 4. Forward encoder
        # feats: (Batch, Length, Dim)
        # -> encoder_out: (Batch, Length2, Dim2)
        encoder_out, encoder_out_lens, _ = self.encoder(feats, feats_lengths)

        # Post-encoder, e.g. NLU
        if self.postencoder is not None:
            encoder_out, encoder_out_lens = self.postencoder(
                encoder_out, encoder_out_lens
            )

        assert encoder_out.size(0) == speech.size(0), (
            encoder_out.size(),
            speech.size(0),
        )
        assert encoder_out.size(1) <= encoder_out_lens.max(), (
            encoder_out.size(),
            encoder_out_lens.max(),
        )

        return encoder_out, encoder_out_lens

    def _extract_feats(
        self, speech: torch.Tensor, speech_lengths: torch.Tensor
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        assert speech_lengths.dim() == 1, speech_lengths.shape

        # for data-parallel
        speech = speech[:, : speech_lengths.max()]

        if self.frontend is not None:
            # Frontend
            #  e.g. STFT and Feature extract
            #       data_loader may send time-domain signal in this case
            # speech (Batch, NSamples) -> feats: (Batch, NFrames, Dim)
            feats, feats_lengths = self.frontend(speech, speech_lengths)
        else:
            # No frontend and no feature extract
            feats, feats_lengths = speech, speech_lengths
        return feats, feats_lengths

    def _calc_mt_att_loss(
        self,
        encoder_out: torch.Tensor,
        encoder_out_lens: torch.Tensor,
        ys_pad: torch.Tensor,
        ys_pad_lens: torch.Tensor,
        speech: Optional[torch.Tensor],
        speech_lens: Optional[torch.Tensor],
    ):
        ys_in_pad, ys_out_pad = add_sos_eos(ys_pad, self.sos, self.eos, self.ignore_id)
        ys_in_lens = ys_pad_lens + 1

        # 1. Forward decoder
        if self.speech_attn:
            decoder_out, _ = self.decoder(
                encoder_out, encoder_out_lens, ys_in_pad, ys_in_lens, speech, speech_lens
            )
        else:
            decoder_out, _ = self.decoder(
                encoder_out, encoder_out_lens, ys_in_pad, ys_in_lens
            )

        # 2. Compute attention loss
        loss_att = self.criterion_st(decoder_out, ys_out_pad)
        acc_att = th_accuracy(
            decoder_out.view(-1, self.vocab_size),
            ys_out_pad,
            ignore_label=self.ignore_id,
        )

        # Compute cer/wer using attention-decoder
        if self.training or self.mt_error_calculator is None:
            bleu_att = None
        else:
            ys_hat = decoder_out.argmax(dim=-1)
            bleu_att = self.mt_error_calculator(ys_hat.cpu(), ys_pad.cpu())

        return loss_att, acc_att, bleu_att

    def _calc_asr_att_loss(
        self,
        encoder_out: torch.Tensor,
        encoder_out_lens: torch.Tensor,
        ys_pad: torch.Tensor,
        ys_pad_lens: torch.Tensor,
    ):
        ys_in_pad, ys_out_pad = add_sos_eos(ys_pad, self.src_sos, self.src_eos, self.ignore_id)
        ys_in_lens = ys_pad_lens + 1

        # 1. Forward decoder
        decoder_out, _, hs_dec_asr = self.asr_decoder(
            encoder_out, encoder_out_lens, ys_in_pad, ys_in_lens, return_hidden=True
        )

        # 2. Compute attention loss
        loss_att = self.criterion_asr(decoder_out, ys_out_pad)
        acc_att = th_accuracy(
            decoder_out.view(-1, self.src_vocab_size),
            ys_out_pad,
            ignore_label=self.ignore_id,
        )

        # Compute cer/wer using attention-decoder
        if self.training or self.asr_error_calculator is None:
            cer_att, wer_att = None, None
        else:
            ys_hat = decoder_out.argmax(dim=-1)
            cer_att, wer_att = self.asr_error_calculator(ys_hat.cpu(), ys_pad.cpu())

        return loss_att, acc_att, cer_att, wer_att, hs_dec_asr

    def _calc_ctc_loss(
        self,
        encoder_out: torch.Tensor,
        encoder_out_lens: torch.Tensor,
        ys_pad: torch.Tensor,
        ys_pad_lens: torch.Tensor,
    ):
        # Calc CTC loss
        loss_ctc = self.ctc(encoder_out, encoder_out_lens, ys_pad, ys_pad_lens)

        # Calc CER using CTC
        cer_ctc = None
        if not self.training and self.asr_error_calculator is not None:
            ys_hat = self.ctc.argmax(encoder_out).data
            cer_ctc = self.asr_error_calculator(ys_hat.cpu(), ys_pad.cpu(), is_ctc=True)
        return loss_ctc, cer_ctc