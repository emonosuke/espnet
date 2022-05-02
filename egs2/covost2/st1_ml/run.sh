#!/usr/bin/env bash
# Set bash to 'debug' mode, it will exit on :
# -e 'error', -u 'undefined variable', -o ... 'error in pipeline', -x 'print commands',
set -e
set -u
set -o pipefail

# language related
lang_pairs="en2de_de2en" # src2tgt_src2tgt_....
# English (en)
# French (fr)
# German (de)
# Spanish (es)
# Catalan (ca)
# Italian (it)
# Russian (ru)
# Chinese (zh-CN)
# Portuguese (pt)
# Persian (fa)
# Estonian (et)
# Mongolian (mn)
# Dutch (nl)
# Turkish (tr)
# Arabic (ar)
# Swedish (sv-SE)
# Latvian (lv)
# Slovenian (sl)
# Tamil (ta)
# Japanese (ja)
# Indonesian (id)
# Welsh (cy)

src_nbpe=1000
tgt_nbpe=1000
src_case=lc.rm
tgt_case=lc.rm

train_set=train.${lang_pairs}
train_dev=dev.${lang_pairs}
test_set="test.${lang_pairs} dev.${lang_pairs}"

st_config=conf/tuning/train_transformer_st.yaml 
inference_config=conf/decode_st.yaml

speed_perturb_factors="0.9 1.0 1.1"

./st_ml.sh \
    --ngpu 1 \
    --local_data_opts "--stage 5 --lang_pairs ${lang_pairs}" \
    --speed_perturb_factors "${speed_perturb_factors}" \
    --use_lm false \
    --feats_type raw \
    --audio_format "flac.ark" \
    --token_joint false \
    --lang_pairs "${lang_pairs}" \
    --src_token_type "bpe" \
    --src_nbpe $src_nbpe \
    --tgt_token_type "bpe" \
    --tgt_nbpe $tgt_nbpe \
    --src_case ${src_case} \
    --tgt_case ${tgt_case} \
    --st_config "${st_config}" \
    --inference_config "${inference_config}" \
    --train_set "${train_set}" \
    --valid_set "${train_dev}" \
    --test_sets "${test_set}" \
    --src_bpe_train_text "data/${train_set}/text.${src_case}.src" \
    --tgt_bpe_train_text "data/${train_set}/text.${tgt_case}.tgt" \
    --lm_train_text "data/${train_set}/text.${tgt_case}.tgt"  "$@"
