"""
Abre o Chrome REAL da máquina (perfil Enzo copiado, extensões, cookies)
com remote-debugging, para o shot_playwright.py se conectar via CDP.

O Chrome NÃO ativa o remote-debugging com o diretório de perfil padrão
("DevTools remote debugging requires a non-default data directory").
Por isso usamos uma CÓPIA do perfil Enzo em chrome_profile_enzo/ e
atualizamos essa cópia sempre que o Chrome real for fechado.

Uso:
    venv/bin/python Captcha/iniciar_chrome.py
"""

import glob
import os
import shutil
import subprocess
import sys
import time
import urllib.request

from janela_chrome import flags_janela, janela_visivel

CHROME = "/opt/google/chrome/google-chrome"
PORTA = 9222
PERFIL_REAL = os.path.expanduser("~/.config/google-chrome")
PERFIL_CDP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "chrome_profile_enzo")
# Perfil real copiado para o diretório custom: Profile 2 = Enzo,
# Profile 1 = Lucas, Default = wagner
PERFIL_FONTE = "Profile 2"
URL = "https://servicos.receita.fazenda.gov.br/Servicos/CPF/ConsultaSituacao/ConsultaPublica.asp"


def cdp_ativo():
    try:
        urllib.request.urlopen(
            "http://127.0.0.1:%d/json/version" % PORTA, timeout=1
        )
        return True
    except Exception:
        return False


def atualizar_copia():
    """Copia o perfil escolhido do Chrome real para o diretório CDP."""
    origem = os.path.join(PERFIL_REAL, PERFIL_FONTE)
    destino = os.path.join(PERFIL_CDP, "Default")
    if not os.path.isdir(origem):
        # servidor nao tem Chrome de verdade instalado com perfil de gente;
        # se a copia ja estiver aqui (veio junto no deploy), ela serve
        if os.path.isdir(destino):
            print("[+] Sem Chrome real nesta máquina; usando a cópia que já existe "
                  "em chrome_profile_enzo/")
            return True
        print("[!] Perfil '%s' não existe em %s" % (PERFIL_FONTE, PERFIL_REAL))
        print("    Em servidor: copie a pasta chrome_profile_enzo/ da máquina "
              "onde o robô já roda.")
        return False
    print("[+] Copiando perfil '%s' -> chrome_profile_enzo/ (cookies atuais)..." %
          PERFIL_FONTE)
    if os.path.isdir(destino):
        shutil.rmtree(destino)
    shutil.copytree(origem, destino, ignore=shutil.ignore_patterns(
        "Singleton*", "*.pma", "Crashpad*", "component_crx_cache"))
    shutil.copyfile(os.path.join(PERFIL_REAL, "Local State"),
                    os.path.join(PERFIL_CDP, "Local State"))
    return True


def main():
    if cdp_ativo():
        print("[+] CDP já ativo na porta %d — navegador pronto." % PORTA)
        print("    Rode: venv/bin/python Captcha/shot_playwright.py")
        return

    if os.path.isdir(PERFIL_REAL) and glob.glob(os.path.join(PERFIL_REAL, "SingletonLock")):
        print("[!] Seu Chrome está ABERTO no momento.")
        print("    Feche todas as janelas dele e rode este comando de novo.")
        print("    (A cópia do perfil só pode ser feita com o Chrome fechado)")
        return

    if not atualizar_copia():
        return

    print("[+] Abrindo Chrome (cópia do perfil '%s') com remote-debugging%s..." %
          (PERFIL_FONTE, "" if janela_visivel() else ", sem janela na tela"))
    subprocess.Popen(
        [
            CHROME,
            "--remote-debugging-port=%d" % PORTA,
            "--remote-allow-origins=*",
            "--user-data-dir=%s" % PERFIL_CDP,
            "--profile-directory=Default",
            *flags_janela(),
            "--lang=pt-BR",
            "--no-first-run",
            URL,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    for _ in range(60):  # espera até 30s o CDP subir
        if cdp_ativo():
            print("[+] CDP ativo! Rode: venv/bin/python Captcha/shot_playwright.py")
            return
        time.sleep(0.5)
    print("[-] O CDP não subiu. Feche o Chrome e tente de novo, ou rode:")
    print("    %s --remote-debugging-port=%d --user-data-dir=%s %s" % (
        CHROME, PORTA, PERFIL_CDP, URL))


if __name__ == "__main__":
    main()