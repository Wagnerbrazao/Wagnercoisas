"""
Rod o fluxo completo de uma vez:
  1. Abre o Chrome REAL da máquina com CDP (se ainda não estiver aberto);
  2. Conecta, preenche CPF/data, resolve o hCaptcha via Groq vision (grátis),
     envia a consulta e espera o "Comprovante de Situação Cadastral no CPF".

Uso:
    venv/bin/python Captcha/rodar_tudo.py
"""

import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import iniciar_chrome  # noqa: E402

PORTA = iniciar_chrome.PORTA
CDP_URL = "http://127.0.0.1:%d" % PORTA
AQUI = os.path.dirname(os.path.abspath(__file__))


def cdp_ativo():
    try:
        urllib.request.urlopen(CDP_URL + "/json/version", timeout=1)
        return True
    except Exception:
        return False


def main():
    print("=" * 60)
    print("FLUXO COMPLETO: Chrome real + consulta CPF")
    print("=" * 60)

    # 1) garante o Chrome real aberto com CDP
    if not cdp_ativo():
        print("\n[1/3] Abrindo seu Chrome real com CDP...")
        iniciar_chrome.main()
        for _ in range(60):
            if cdp_ativo():
                break
            time.sleep(0.5)
        if not cdp_ativo():
            print("[-] Chrome não subiu com CDP. Feche o Chrome e rode de novo.")
            return
    else:
        print("\n[1/3] Chrome real já está com CDP ativo.")

    # 2) roda o fluxo da consulta
    print("\n[2/3] Rodando o fluxo da consulta (shot_groq)...")
    print("      Resolvendo hCaptcha via Gemini vision...\n")
    env = os.environ.copy()
    if "GEMINI_API_KEY" not in env:
        print("[-] Defina GEMINI_API_KEY: export GEMINI_API_KEY=...")
        return
    r = subprocess.run(
        [sys.executable, os.path.join(AQUI, "shot_groq.py")],
        cwd=AQUI,
        env=env,
    )
    if r.returncode != 0:
        print("[-] shot_groq.py terminou com erro (%d)." % r.returncode)

    # 3) encerra
    print("\n[3/3] Fim. Seu Chrome continua aberto — pode fechá-lo normalmente.")


if __name__ == "__main__":
    main()