"""
Vigia CSVs de datas de nascimento que ainda estao sendo gerados e baixa o
comprovante da Receita de cada CPF novo que aparecer.

A cada rodada:
  1. junta todos os CSVs (cpf;nome;data_nascimento;...), sem repetir CPF;
  2. tira quem ja tem PDF na pasta de saida e quem ja esgotou as tentativas;
  3. roda o baixar_lote.py so com os pendentes;
  4. se nao ha pendentes, espera e olha de novo - ate os CSVs pararem de crescer.

Uso:
    venv/bin/python -u vigiar_datas.py
"""

import argparse
import csv
import glob
import os
import re
import subprocess
import sys
import time

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)

from agendar_lote import aviso, garantir_chrome  # noqa: E402
from baixar_lote import ja_baixado  # noqa: E402
from autenticar_rede import carregar_env, uma_rodada as autenticar  # noqa: E402
from aquecer import aquecer  # noqa: E402

ENTRADAS = "/home/wagner/Downloads/carga_csv/datas_nascimento_*.csv"
SAIDA = "/home/wagner/Downloads/carga_csv/comprovantes_datas_nascimento"


def ler_entradas(padrao):
    """CPF -> (data, nome). Em CPF repetido vale a ultima data valida lida."""
    registros = {}
    for arq in sorted(glob.glob(padrao)):
        try:
            with open(arq, newline="", encoding="utf-8-sig") as fh:
                for r in csv.DictReader(fh, delimiter=";"):
                    cpf = re.sub(r"\D", "", r.get("cpf") or "")
                    data = (r.get("data_nascimento") or "").strip()
                    if not cpf or not re.match(r"^\d{2}/\d{2}/\d{4}$", data):
                        continue
                    registros[cpf.zfill(11)] = (data, (r.get("nome") or "").strip())
        except (OSError, csv.Error):
            continue  # arquivo sendo escrito neste instante: pega na proxima
    return registros


def falhas(saida):
    """(cpf, data) -> quantas vezes nao deu certo, lido do relatorio."""
    contagem = {}
    try:
        with open(os.path.join(saida, "relatorio.csv"), newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh, delimiter=";"):
                # so conta falha do proprio CPF; queda de rede ou do Chrome nao e culpa dele
                if r.get("status") in ("captcha_falhou", "dados_invalidos"):
                    chave = (r.get("cpf", ""), r.get("data_nascimento", ""))
                    contagem[chave] = contagem.get(chave, 0) + 1
    except FileNotFoundError:
        pass
    return contagem


def assinatura(padrao):
    """Muda sempre que algum CSV de entrada cresce ou e reescrito."""
    return tuple((f, os.path.getsize(f), os.path.getmtime(f)) for f in sorted(glob.glob(padrao)))


def main():
    ap = argparse.ArgumentParser(description="Vigia CSVs de datas e baixa os comprovantes.")
    ap.add_argument("--entradas", default=ENTRADAS, help="glob dos CSVs de entrada")
    ap.add_argument("--saida", default=SAIDA)
    ap.add_argument("--checar-a-cada", type=float, default=10, dest="checar",
                    help="minutos entre olhadas quando nao ha pendentes")
    ap.add_argument("--parar-apos", type=float, default=120, dest="parar",
                    help="encerra se os CSVs ficarem este tanto de minutos sem mudar")
    ap.add_argument("--max-falhas", type=int, default=3,
                    help="desiste de um CPF depois de tantas falhas com a mesma data")
    ap.add_argument("--intervalo", type=float, default=25)
    args = ap.parse_args()

    carregar_env()
    os.makedirs(args.saida, exist_ok=True)
    fila = os.path.join(args.saida, "_fila_atual.csv")
    ultima_mudanca, ultima_assinatura = time.time(), None

    while True:
        sig = assinatura(args.entradas)
        if sig != ultima_assinatura:
            ultima_assinatura, ultima_mudanca = sig, time.time()

        registros = ler_entradas(args.entradas)
        erros = falhas(args.saida)
        pendentes = [(c, d, n) for c, (d, n) in sorted(registros.items())
                     if not ja_baixado(args.saida, c) and erros.get((c, d), 0) < args.max_falhas]
        prontos = sum(1 for c in registros if ja_baixado(args.saida, c))
        aviso("CSVs: %d CPFs com data | ja baixados: %d | pendentes: %d"
              % (len(registros), prontos, len(pendentes)))

        if pendentes:
            with open(fila, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh, delimiter=";")
                w.writerow(["cpf", "data_nascimento", "nome"])
                w.writerows(pendentes)
            try:
                autenticar()          # sessao do captive pode ter expirado
            except Exception as e:
                aviso("Autenticacao do captive falhou: %s" % str(e)[:80])
            if garantir_chrome():
                # perfil frio tira nota no hCaptcha; alguns minutos de navegacao
                # normal antes do lote valem mais que tentativa extra de captcha
                aquecer(3, (25, 55))
                env = dict(os.environ)
                for linha in open(os.path.join(AQUI, ".env")):
                    linha = linha.strip()
                    if "=" in linha and not linha.startswith("#"):
                        k, v = linha.split("=", 1)
                        env[k] = v
                subprocess.run([sys.executable, "-u", os.path.join(AQUI, "baixar_lote.py"),
                                "--entrada", fila, "--saida", args.saida,
                                "--intervalo", str(args.intervalo),
                                "--tempo-max", "180", "--repescagens", "1"],
                               cwd=AQUI, env=env)
            continue  # olha de novo na hora: pode ter chegado CPF enquanto rodava

        parado = (time.time() - ultima_mudanca) / 60
        if parado >= args.parar:
            aviso("CSVs sem mudar ha %.0f min e nada pendente. Encerrando." % parado)
            return
        aviso("Nada pendente; olho de novo em %g min (CSVs parados ha %.0f min)."
              % (args.checar, parado))
        time.sleep(args.checar * 60)


if __name__ == "__main__":
    main()
