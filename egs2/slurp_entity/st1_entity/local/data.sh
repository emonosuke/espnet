#!/usr/bin/env bash
# Set bash to 'debug' mode, it will exit on :
# -e 'error', -u 'undefined variable', -o ... 'error in pipeline', -x 'print commands',
set -e
set -u
set -o pipefail

log() {
    local fname=${BASH_SOURCE[1]##*/}
    echo -e "$(date '+%Y-%m-%dT%H:%M:%S') (${fname}:${BASH_LINENO[0]}:${FUNCNAME[1]}) $*"
}
SECONDS=0


stage=1
stop_stage=100000
log "$0 $*"
. utils/parse_options.sh

. ./db.sh
. ./path.sh
. ./cmd.sh

if [ $# -ne 0 ]; then
    log "Error: No positional arguments are required."
    exit 2
fi

if [ -z "${SLURP}" ]; then
    log "Fill the value of 'SLURP' of db.sh"
    exit 1
fi

if [ ${stage} -le 1 ] && [ ${stop_stage} -ge 1 ]; then
    if [ ! -e "${SLURP}/LICENSE.txt" ]; then
	echo "stage 1: Download data to ${SLURP}"
    else
        log "stage 1: ${SLURP}/LICENSE.txt is already existing. Skip data downloading"
    fi
fi

if [ ${stage} -le 2 ] && [ ${stop_stage} -ge 2 ]; then
    log "stage 2: Data Preparation"
    mkdir -p data/{train,valid,test}
    python3 local/prepare_slurp_entity_data_token_md.py ${SLURP}
    local/run_spm.sh
    mv data data_old
    mv data_bpe_500 data
    python3 local/create_ner_subtoken_file.py 
    for x in test devel train; do
        cp data/${x}/text.ner.en data/${x}/text.ner.en.old
        cp data/${x}/text_subtoken.ner.en data/${x}/text.ner.en
        for f in text.asr.en text.ner.en wav.scp utt2spk; do
            sort data/${x}/${f} -o data/${x}/${f}
        done
        cp data/${x}/text.ner.en data/${x}/text
        utils/utt2spk_to_spk2utt.pl data/${x}/utt2spk > "data/${x}/spk2utt"
        utils/validate_data_dir.sh --no-feats data/${x} || exit 1
    done
fi

log "Successfully finished. [elapsed=${SECONDS}s]"