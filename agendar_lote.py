"""
Agendador do lote de comprovantes, para rodar sozinho no fim de semana.

Faz tres coisas que o lote sozinho nao faz:
  1. respeita uma pausa inicial (--pausa) antes da primeira execucao;
  2. garante o Chrome com CDP no ar antes de cada execucao - foi ele que
     derrubou o lote algumas vezes ao fechar sozinho;
  3. depois da primeira execucao, roda uma vez por madrugada, quando a cota
     diaria do Gemini reseta (~04h de Brasilia / meia-noite no Pacifico).

Uso:
    setsid nohup venv/bin/python -u agendar_lote.py --pausa 120 --chave 2 &
"""

import argparse
import datetime as dt
import os
import subprocess
import sys
import time
import urllib.request

from janela_chrome import flags_janela

AQUI = os.path.dirname(os.path.abspath(__file__))
CDP_URL = "http://127.0.0.1:9222"
CHROME = "/opt/google/chrome/google-chrome"
PERFIL = os.path.join(os.path.dirname(AQUI), "chrome_profile_enzo")
URL = "https://servicos.receita.fazenda.gov.br/Servicos/CPF/ConsultaSituacao/ConsultaPublica.asp"
SAIDA = "/home/wagner/Downloads/carga_csv/comprovantes"


def agora():
    return dt.datetime.now().strftime("%d/%m %H:%M")


def aviso(msg):
    print("[agenda %s] %s" % (agora(), msg), flush=True)


def cdp_ativo():
    try:
        urllib.request.urlopen(CDP_URL + "/json/version", timeout=2)
        return True
    except Exception:
        return False


def garantir_chrome():
    """Sobe o Chrome com CDP se ele nao estiver de pe. True se ficou pronto."""
    if cdp_ativo():
        return True
    aviso("Chrome sem CDP; subindo de novo.")
    subprocess.Popen(
        [CHROME, "--remote-debugging-port=9222", "--remote-allow-origins=*",
         "--user-data-dir=%s" % PERFIL, "--profile-directory=Default"]
        + flags_janela() + ["--lang=pt-BR", "--no-first-run", URL],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(60):
        if cdp_ativo():
            aviso("Chrome pronto.")
            return True
        time.sleep(1)
    aviso("Chrome nao subiu; pulando esta execucao.")
    return False


def quantos_pdfs():
    try:
        return len([n for n in os.listdir(SAIDA) if n.endswith(".pdf")])
    except Exception:
        return -1


def rodar(chave_primeira):
    if not garantir_chrome():
        return
    env = dict(os.environ)
    for linha in open(os.path.join(AQUI, ".env")):
        linha = linha.strip()
        if "=" in linha and not linha.startswith("#"):
            k, v = linha.split("=", 1)
            env[k] = v
    if chave_primeira:
        env["GEMINI_CHAVE_PRIMEIRA"] = str(chave_primeira)
    antes = quantos_pdfs()
    aviso("Iniciando o lote (chave %s na frente). PDFs agora: %d"
          % (chave_primeira or 1, antes))
    subprocess.run([os.path.join(AQUI, "rodar_lote_completo.sh")], cwd=AQUI, env=env)
    aviso("Lote encerrou. PDFs: %d (+%d nesta execucao)"
          % (quantos_pdfs(), quantos_pdfs() - antes))


def segundos_ate(hora, minuto):
    """Segundos ate o proximo horario hora:minuto (hoje ou amanha)."""
    agora_dt = dt.datetime.now()
    alvo = agora_dt.replace(hour=hora, minute=minuto, second=0, microsecond=0)
    if alvo <= agora_dt:
        alvo += dt.timedelta(days=1)
    return (alvo - agora_dt).total_seconds(), alvo


def main():
    ap = argparse.ArgumentParser(description="Agenda o lote de comprovantes.")
    ap.add_argument("--pausa", type=float, default=120,
                    help="minutos de pausa antes da primeira execucao (padrao 120)")
    ap.add_argument("--chave", type=int, default=0,
                    help="numero da chave que entra primeiro (ex.: 2)")
    ap.add_argument("--madrugada", default="04:10",
                    help="horario da execucao diaria, apos o reset da cota (HH:MM)")
    args = ap.parse_args()

    if args.pausa > 0:
        _, volta = segundos_ate(*[int(x) for x in
                                  (dt.datetime.now() + dt.timedelta(minutes=args.pausa)
                                   ).strftime("%H:%M").split(":")])
        aviso("Pausa de %g min. Volto por volta de %s." % (args.pausa, volta.strftime("%H:%M")))
        time.sleep(args.pausa * 60)

    rodar(args.chave)

    hora, minuto = [int(x) for x in args.madrugada.split(":")]
    while True:
        espera, alvo = segundos_ate(hora, minuto)
        aviso("Proxima execucao da madrugada: %s (em %.1f h)."
              % (alvo.strftime("%d/%m %H:%M"), espera / 3600))
        time.sleep(espera)
        rodar(0)  # cota renovada: comeca pela chave 1 de novo


if __name__ == "__main__":
    main()
