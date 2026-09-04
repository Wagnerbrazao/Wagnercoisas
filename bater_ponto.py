"""
Registra o ponto no SPE (Sistema de Ponto Eletronico) do TJGO.

Faz o mesmo caminho que se faz no navegador:

  1. autentica em portaltj.tjgo.jus.br (Acesso Corporativo, usuario do AD);
  2. abre index.php?s=ponto, que carrega o registro dentro de um iframe;
  3. le a classe do botao (btn_ponto_entrada / btn_ponto_saida) para saber
     qual e o proximo registro;
  4. pede confirmacao, clica, e confere a "frequencia do dia" depois.

Credenciais vem do .env (PONTO_USER / PONTO_SENHA), nao ficam no codigo.

IMPORTANTE: por padrao ele SEMPRE pergunta antes de clicar, porque o clique
grava um registro oficial de frequencia e desfazer exige abrir ocorrencia no
RH. O --sim existe para uso consciente; nao coloque este script no cron para
bater ponto sozinho em horario fixo, porque ai o registro deixa de refletir
presenca real.

Uso:
    venv/bin/python bater_ponto.py                    # mostra o estado, pergunta e registra
    venv/bin/python bater_ponto.py --so-ver           # so consulta, nunca clica
    venv/bin/python bater_ponto.py --hora 19:00       # espera ate 19:00 e ai pergunta
    venv/bin/python bater_ponto.py --hora 19:00 --sim # espera e registra sem perguntar
    venv/bin/python bater_ponto.py --esperado saida   # aborta se o proximo nao for saida
"""

import argparse
import datetime
import os
import sys
import time

from playwright.sync_api import sync_playwright

AQUI = os.path.dirname(os.path.abspath(__file__))

URL_LOGIN = "https://portaltj.tjgo.jus.br/sistemas/index.php?s=corporativo&c=ct_autenticacao"
URL_PONTO = "https://portaltj.tjgo.jus.br/sistemas/index.php?s=ponto"

# o registro mora dentro deste iframe; o botao e uma div com onclick="registrar()"
SEL_IFRAME = "#iframe_conteudo"
SEL_BOTAO = "div[class^='btn_ponto']"


def carregar_env():
    caminho = os.path.join(AQUI, ".env")
    if not os.path.exists(caminho):
        return
    for linha in open(caminho):
        linha = linha.strip()
        if "=" in linha and not linha.startswith("#"):
            chave, valor = linha.split("=", 1)
            os.environ.setdefault(chave, valor)


def esperar_ate(hhmm):
    """Dorme ate o HH:MM de hoje. Se ja passou, segue direto."""
    try:
        hora, minuto = [int(x) for x in hhmm.split(":")]
    except ValueError:
        print("[-] Hora invalida: %r (use HH:MM)." % hhmm)
        sys.exit(2)

    agora = datetime.datetime.now()
    alvo = agora.replace(hour=hora, minute=minuto, second=0, microsecond=0)
    faltam = (alvo - agora).total_seconds()
    if faltam <= 0:
        print("[i] %s ja passou (agora sao %s); seguindo." % (hhmm, agora.strftime("%H:%M:%S")))
        return

    print("[i] Esperando ate %s (%d min). Ctrl+C para desistir." % (hhmm, round(faltam / 60)))
    # acorda um pouco antes para logar com folga, depois ajusta no fim
    time.sleep(max(0, faltam - 45))
    while datetime.datetime.now() < alvo:
        time.sleep(0.2)


def autenticar(page, usuario, senha):
    page.goto(URL_LOGIN, wait_until="domcontentloaded")
    # a sessao pode ja estar viva; nesse caso nem aparece formulario
    if page.locator("input[type='password']").count() == 0:
        print("[+] Sessao ja autenticada.")
        return True
    page.fill("form input[type='text']", usuario)
    page.fill("form input[type='password']", senha)
    page.click("form button")
    page.wait_for_load_state("domcontentloaded")
    if page.locator("input[type='password']").count() > 0:
        print("[-] Login recusado. Confira PONTO_USER / PONTO_SENHA no .env.")
        return False
    print("[+] Autenticado como %s." % usuario)
    return True


def abrir_ponto(page):
    page.goto(URL_PONTO, wait_until="domcontentloaded")
    if "ct_autenticacao" in page.url:
        print("[-] Caiu de volta no login: a sessao expirou.")
        return None
    quadro = page.frame_locator(SEL_IFRAME)
    quadro.locator(SEL_BOTAO).wait_for(state="attached", timeout=15000)
    return quadro


def ler_estado(quadro):
    """Devolve ('entrada'|'saida'|None, texto da frequencia do dia)."""
    classe = quadro.locator(SEL_BOTAO).get_attribute("class") or ""
    proximo = None
    if "btn_ponto_entrada" in classe:
        proximo = "entrada"
    elif "btn_ponto_saida" in classe:
        proximo = "saida"

    linhas = []
    marcacoes = quadro.locator("text=/^(ENTRADA|SAIDA|SAÍDA)$/")
    for i in range(marcacoes.count()):
        linhas.append(marcacoes.nth(i).text_content().strip())
    frequencia = quadro.locator("#tabela_frequencia")
    texto = frequencia.inner_text().strip() if frequencia.count() else " / ".join(linhas)
    return proximo, texto


def registrar(quadro, page, destino_print):
    quadro.locator(SEL_BOTAO).click()
    page.wait_for_load_state("networkidle", timeout=20000)
    time.sleep(1)
    page.screenshot(path=destino_print)
    return abrir_ponto(page) or quadro


def main():
    ap = argparse.ArgumentParser(description="Registra o ponto no SPE do TJGO.")
    ap.add_argument("--hora", metavar="HH:MM",
                    help="espera ate esse horario de hoje antes de registrar")
    ap.add_argument("--esperado", choices=["entrada", "saida"],
                    help="so registra se o proximo ponto for esse; senao aborta")
    ap.add_argument("--sim", action="store_true",
                    help="nao pergunta antes de clicar (use com consciencia)")
    ap.add_argument("--so-ver", action="store_true",
                    help="apenas mostra o estado atual, nunca clica")
    ap.add_argument("--visivel", action="store_true",
                    help="abre o Chrome na tela em vez de headless")
    ap.add_argument("--print", dest="destino_print", default=None,
                    help="caminho do screenshot (padrao: ponto_AAAA-MM-DD_HHMM.png)")
    args = ap.parse_args()

    carregar_env()
    usuario = os.environ.get("PONTO_USER", "")
    senha = os.environ.get("PONTO_SENHA", "")
    if not (usuario and senha):
        print("[-] Defina PONTO_USER e PONTO_SENHA no .env.")
        sys.exit(2)

    if args.hora and not args.so_ver:
        esperar_ate(args.hora)

    destino = args.destino_print or os.path.join(
        AQUI, "ponto_%s.png" % datetime.datetime.now().strftime("%Y-%m-%d_%H%M"))

    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=not args.visivel)
        page = navegador.new_page(viewport={"width": 1600, "height": 900})
        try:
            if not autenticar(page, usuario, senha):
                sys.exit(1)

            quadro = abrir_ponto(page)
            if quadro is None:
                sys.exit(1)

            proximo, frequencia = ler_estado(quadro)
            print("[i] Proximo registro: %s" % (proximo or "indefinido"))
            print("[i] Frequencia do dia:\n%s" % (frequencia or "(vazia)"))

            if args.so_ver:
                page.screenshot(path=destino)
                print("[+] Print em %s (nada foi registrado)." % destino)
                return

            if proximo is None:
                print("[-] Nao consegui identificar o botao; nao vou clicar no escuro.")
                sys.exit(1)

            if args.esperado and proximo != args.esperado:
                print("[-] Esperava '%s' mas o proximo e '%s'. Abortando." % (args.esperado, proximo))
                sys.exit(1)

            if not args.sim:
                agora = datetime.datetime.now().strftime("%H:%M:%S")
                resposta = input("[?] Registrar %s agora (%s)? [s/N] " % (proximo.upper(), agora))
                if resposta.strip().lower() not in ("s", "sim"):
                    print("[i] Cancelado; nada foi registrado.")
                    return

            print("[+] Registrando %s..." % proximo.upper())
            quadro = registrar(quadro, page, destino)

            novo_proximo, nova_frequencia = ler_estado(quadro)
            print("[i] Frequencia do dia agora:\n%s" % (nova_frequencia or "(vazia)"))
            if novo_proximo and novo_proximo != proximo:
                print("[+] Registrado: o botao virou '%s'. Print em %s" % (novo_proximo, destino))
            else:
                print("[-] O botao continua '%s'; confira o print em %s." % (novo_proximo, destino))
                sys.exit(1)
        finally:
            navegador.close()


if __name__ == "__main__":
    main()
