"""
Autentica a maquina no captive portal do TJGO.

Equivale a funcao captive() que existe espalhada em novo_CESNA.py, robo_seeu.py,
PROJUDI.py e outros, com tres diferencas:

  * credenciais vem do .env (PORTAL_USER / PORTAL_SENHA), nao ficam no codigo;
  * so autentica se a rede estiver mesmo bloqueada, em vez de sempre abrir o portal;
  * reaproveita o Chrome que ja esta com CDP, quando ha um.

Uso:
    venv/bin/python autenticar_rede.py              # autentica se precisar
    venv/bin/python autenticar_rede.py --forcar     # autentica mesmo ja conectado
    venv/bin/python autenticar_rede.py --repetir 240  # revalida a cada 4h (como o schedule antigo)
"""

import argparse
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
import warnings

warnings.filterwarnings("ignore")

from playwright.sync_api import sync_playwright

AQUI = os.path.dirname(os.path.abspath(__file__))
CDP_URL = "http://127.0.0.1:9222"
# alvo leve so para saber se a rede esta liberada
TESTE_URL = "https://servicos.receita.fazenda.gov.br/Servicos/CPF/ConsultaSituacao/ConsultaPublica.asp"


def carregar_env():
    caminho = os.path.join(AQUI, ".env")
    if not os.path.exists(caminho):
        return
    for linha in open(caminho):
        linha = linha.strip()
        if "=" in linha and not linha.startswith("#"):
            chave, valor = linha.split("=", 1)
            os.environ.setdefault(chave, valor)


def conectado(timeout=6):
    """True se a rede ja esta liberada (sem redirecionamento para o portal)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        resp = urllib.request.urlopen(TESTE_URL, timeout=timeout, context=ctx)
        destino = resp.geturl()
        # o portal sequestra a navegacao: se caiu nele, ainda nao ha rede
        return "captiveportal" not in destino
    except urllib.error.HTTPError:
        return True  # respondeu algo do proprio site: ha rede
    except Exception:
        return False


def cdp_ativo():
    try:
        urllib.request.urlopen(CDP_URL + "/json/version", timeout=2)
        return True
    except Exception:
        return False


def autenticar(page, url, usuario, senha):
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_selector("#submit", state="visible", timeout=15000)
    page.fill("#user", usuario)
    page.fill("#passwd", senha)
    page.click("#submit")
    page.wait_for_load_state("networkidle", timeout=15000)
    time.sleep(2)


def uma_rodada(forcar=False):
    url = os.environ.get("PORTAL_URL", "")
    usuario = os.environ.get("PORTAL_USER", "")
    senha = os.environ.get("PORTAL_SENHA", "")
    if not (url and usuario and senha):
        print("[-] Defina PORTAL_URL, PORTAL_USER e PORTAL_SENHA no .env.")
        return False

    if not forcar and conectado():
        print("[+] Rede ja liberada; nao precisa autenticar.")
        return True

    print("[+] Autenticando em %s como %s..." % (url, usuario))
    with sync_playwright() as p:
        if cdp_ativo():
            # reaproveita o Chrome do lote, numa aba propria que e fechada depois
            navegador = p.chromium.connect_over_cdp(CDP_URL)
            ctx = navegador.contexts[0]
            page = ctx.new_page()
            fechar_navegador = False
        else:
            navegador = p.chromium.launch(headless=True)
            page = navegador.new_page()
            fechar_navegador = True
        try:
            autenticar(page, url, usuario, senha)
        except Exception as e:
            print("[-] Falhou autenticar: %s" % str(e).splitlines()[0][:120])
            return False
        finally:
            try:
                page.close()
            except Exception:
                pass
            if fechar_navegador:
                navegador.close()

    if conectado():
        print("[+] Autenticado: rede liberada.")
        return True
    print("[-] Enviou o formulario mas a rede continua bloqueada.")
    return False


def main():
    ap = argparse.ArgumentParser(description="Autentica no captive portal do TJGO.")
    ap.add_argument("--forcar", action="store_true",
                    help="autentica mesmo se a rede ja estiver liberada")
    ap.add_argument("--repetir", type=float, default=0, metavar="MIN",
                    help="revalida a cada MIN minutos, em vez de sair (ex.: 240 = 4h)")
    args = ap.parse_args()

    carregar_env()

    if not args.repetir:
        sys.exit(0 if uma_rodada(args.forcar) else 1)

    print("[i] Revalidando a cada %g min. Ctrl+C para sair." % args.repetir)
    while True:
        try:
            uma_rodada(args.forcar)
        except KeyboardInterrupt:
            print("\n[i] Encerrado.")
            return
        except Exception as e:
            print("[-] Erro na rodada: %s" % str(e)[:120])
        time.sleep(args.repetir * 60)


if __name__ == "__main__":
    main()
