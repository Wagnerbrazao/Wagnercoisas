"""
Aquece o perfil do Chrome antes do lote.

O hCaptcha nao olha so o desafio: ele pontua o navegador pelo historico de
uso (cookies dos sites grandes, tempo de pagina, rolagem, mouse). Um perfil
que so abre a pagina da Receita, resolve captcha e sai e o retrato de um robo,
e a nota baixa cobra desafio mais dificil - as tentativas de 200s do log.

Aqui o navegador passa alguns minutos como gente: abre YouTube e companhia em
uma aba separada, rola a pagina, mexe o mouse, fica parado lendo, e fecha a
aba. Nada e enviado, nada e clicado; o objetivo e so o perfil nao chegar frio
na consulta.

Uso:
    venv/bin/python aquecer.py            # uma rodada (~2 min)
    venv/bin/python aquecer.py --sites 5  # mais demorado
"""

import argparse
import random
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

CDP_URL = "http://127.0.0.1:9222"

SITES = [
    "https://www.youtube.com/",
    "https://www.google.com/",
    "https://pt.wikipedia.org/wiki/Especial:Aleat%C3%B3ria",
    "https://g1.globo.com/",
    "https://www.uol.com.br/",
    "https://www.climatempo.com.br/",
]


def cdp_ativo():
    try:
        urllib.request.urlopen(CDP_URL + "/json/version", timeout=2)
        return True
    except Exception:
        return False


def passear(page, url, segundos):
    """Abre uma pagina e se comporta como leitor ate o tempo acabar."""
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    fim = time.time() + segundos
    while time.time() < fim:
        acao = random.random()
        if acao < 0.6:
            page.mouse.wheel(0, random.randint(200, 700))
        elif acao < 0.8:
            page.mouse.wheel(0, -random.randint(100, 400))
        else:
            page.mouse.move(random.randint(100, 1500), random.randint(100, 800),
                            steps=random.randint(5, 15))
        time.sleep(random.uniform(1.5, 4.0))


def aquecer(quantos=3, por_site=(25, 55), silencioso=False):
    """Visita alguns sites comuns no mesmo perfil. Nunca levanta excecao."""
    def diz(msg):
        if not silencioso:
            print("[aquece] %s" % msg, flush=True)

    if not cdp_ativo():
        diz("Chrome sem CDP; nada a aquecer.")
        return False

    escolhidos = random.sample(SITES, min(quantos, len(SITES)))
    try:
        with sync_playwright() as p:
            navegador = p.chromium.connect_over_cdp(CDP_URL)
            ctx = navegador.contexts[0]
            page = ctx.new_page()
            try:
                for url in escolhidos:
                    segundos = random.uniform(*por_site)
                    diz("%s (%ds)" % (url.split("/")[2], segundos))
                    try:
                        passear(page, url, segundos)
                    except Exception as e:
                        diz("falhou em %s: %s" % (url.split("/")[2], str(e)[:60]))
            finally:
                try:
                    page.close()
                except Exception:
                    pass
            # devolve o foco para a aba da Receita, que e quem o lote usa
            alvo = next((pg for pg in ctx.pages
                         if "servicos.receita.fazenda.gov.br" in pg.url), None)
            if alvo:
                try:
                    alvo.bring_to_front()
                except Exception:
                    pass
        diz("perfil aquecido com %d site(s)." % len(escolhidos))
        return True
    except Exception as e:
        diz("nao deu para aquecer: %s" % str(e)[:80])
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sites", type=int, default=3, help="quantos sites visitar (padrao 3)")
    ap.add_argument("--min", type=float, default=25, help="segundos minimos por site")
    ap.add_argument("--max", type=float, default=55, help="segundos maximos por site")
    args = ap.parse_args()
    ok = aquecer(args.sites, (args.min, args.max))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
