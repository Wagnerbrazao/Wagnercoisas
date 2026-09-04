#!/usr/bin/env bash
# Dispara o lote completo dos comprovantes de situacao cadastral.
# Sobrevive ao fechamento do terminal (nohup/setsid). Retoma de onde parou.
cd "$(dirname "$0")" || exit 1
set -a; . ./.env; set +a

# libera a rede antes de comecar; sai na hora se ja estiver liberada
venv/bin/python autenticar_rede.py || echo "[!] Seguindo mesmo sem autenticar."

exec venv/bin/python -u baixar_lote.py \
    --entrada /home/wagner/Downloads/carga_csv/cpfs_com_data.csv \
    --saida   /home/wagner/Downloads/carga_csv/comprovantes \
    --intervalo 25 \
    --tentativas 8
