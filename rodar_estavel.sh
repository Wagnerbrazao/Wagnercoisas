#!/usr/bin/env bash
# Versao estavel (a que baixou os 86): so rodada 1, sem o reload que quebrou.
cd "$(dirname "$0")" || exit 1
set -a; . ./.env; set +a
exec venv/bin/python -u baixar_lote_estavel.py \
    --entrada /home/wagner/Downloads/carga_csv/cpfs_com_data.csv \
    --saida   /home/wagner/Downloads/carga_csv/comprovantes \
    --intervalo 8 \
    --tentativas 5 --corte 4 --rodadas 1
