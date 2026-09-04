"""
Versao do lote que usa o caminho do captcha1.py: SeleniumBase em CDP Mode.

A ideia nao e "resolver" o hCaptcha por visao, e sim ter REPUTACAO de sessao
boa o bastante para o checkbox passar direto, sem desafio de imagens - que e
justamente onde a versao com Gemini empacou (imagens adulteradas de proposito).

Diferencas para o baixar_lote.py:
  - dirige o Chrome pelo CDP Mode do SeleniumBase (sem fingerprint de WebDriver);
  - perfil persistente proprio, que acumula reputacao a cada execucao;
  - clique no checkbox com mouse REAL (pyautogui), quando o display permite;
  - se o desafio de imagens aparecer, ESPERA voce resolver na janela
    (--espera segundos) em vez de tentar adivinhar.

Uso:
    venv/bin/python baixar_lote_cdp.py \
        --entrada /home/wagner/Downloads/carga_csv/cpfs_com_data.csv \
        --saida   /home/wagner/Downloads/carga_csv/comprovantes
"""

import argparse
import base64
import csv
import os
import sys
import time

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)

from seleniumbase import SB  # noqa: E402

from baixar_lote import ler_entrada  # noqa: E402  (mesmo parser de CSV)
from movimento_humano import clicar_checkbox_humano, pyautogui_ok  # noqa: E402

CHROME_BINARY = "/opt/google/chrome/google-chrome"
# Perfil dedicado e persistente: quanto mais usado, melhor a pontuacao no hCaptcha.
PERFIL = os.path.join(os.path.dirname(AQUI), "chrome_profile_rf")
URL = "https://servicos.receita.fazenda.gov.br/Servicos/CPF/ConsultaSituacao/ConsultaPublica.asp"


def token_hcaptcha(sb):
    return sb.cdp.evaluate(
        "(function(){var e=document.querySelector("
        "\"textarea[name='h-captcha-response']\");return e?e.value:'';})();"
    ) or ""


def aguardar_token(sb, timeout):
    fim = time.time() + timeout
    while time.time() < fim:
        if token_hcaptcha(sb):
            return True
        time.sleep(2)
    return False


def preencher_campo(sb, campo_id, valor):
    sb.cdp.type("#" + campo_id, valor)
    sb.cdp.evaluate("document.getElementById('%s').blur();" % campo_id)
    sb.sleep(0.6)


def preparar_envio(sb):
    """Espelha o token no campo que a pagina valida (mesma logica do captcha1.py)."""
    return sb.cdp.evaluate(r"""
        (function () {
            var ta = document.querySelector("textarea[name='h-captcha-response']");
            var token = ta ? ta.value : "";
            var chk = document.getElementsByName("idCheckedReCaptcha")[0];
            if (chk) chk.value = "true";
            var alvo = document.getElementById("h-recaptcha-response");
            if (!alvo) {
                alvo = document.createElement("input");
                alvo.type = "hidden";
                alvo.id = "h-recaptcha-response";
                alvo.name = "h-recaptcha-response";
                (document.getElementById("theForm") || document.body).appendChild(alvo);
            }
            alvo.value = token;
            return token.length;
        })();
    """)


def clicar_checkbox(sb):
    sb.cdp.wait_for_element_visible(".h-captcha iframe", timeout=15)
    sb.sleep(2)
    rect = sb.cdp.get_gui_element_rect(".h-captcha iframe")
    x = rect["x"] + 32
    y = rect["y"] + rect["height"] / 2
    pg = pyautogui_ok()
    if pg:
        clicar_checkbox_humano(pg, x, y)
    else:
        sb.cdp.click(".h-captcha iframe")


def salvar_pdf(sb, caminho):
    """printToPDF pelo driver; se nao der, guarda o HTML do comprovante."""
    try:
        res = sb.driver.execute_cdp_cmd("Page.printToPDF", {
            "printBackground": True,
            "marginTop": 0.4, "marginBottom": 0.4,
            "marginLeft": 0.4, "marginRight": 0.4,
        })
        with open(caminho, "wb") as fh:
            fh.write(base64.b64decode(res["data"]))
        return caminho
    except Exception:
        alternativo = caminho[:-4] + ".html"
        with open(alternativo, "w", encoding="utf-8") as fh:
            fh.write(sb.cdp.get_page_source())
        return alternativo


def consultar(sb, registro, saida, espera):
    cpf, data = registro["cpf"], registro["data"]
    destino = os.path.join(saida, "%s.pdf" % cpf)

    sb.cdp.open(URL)
    sb.sleep(1.5)
    preencher_campo(sb, "txtCPF", cpf)
    preencher_campo(sb, "txtDataNascimento", data)

    try:
        clicar_checkbox(sb)
    except Exception as e:
        print("    [!] Clique no checkbox falhou: %s" % str(e)[:80])

    if not aguardar_token(sb, espera):
        return "sem_token", ""

    if not preparar_envio(sb):
        return "token_vazio", ""

    sb.cdp.click("#id_submit")
    sb.sleep(3)

    if "ConsultaPublicaExibir" in sb.cdp.get_current_url():
        return "ok", salvar_pdf(sb, destino)

    texto = sb.cdp.get_text("body")[:3000]
    for aviso in ("Anti-Rob", "Data de nascimento", "CPF informado", "nao existe"):
        if aviso.lower() in texto.lower():
            return "recusado", aviso
    return "sem_resultado", ""


def main():
    ap = argparse.ArgumentParser(description="Lote via SeleniumBase CDP Mode (reputacao de sessao).")
    ap.add_argument("--entrada", required=True)
    ap.add_argument("--saida", default=os.path.join(AQUI, "comprovantes"))
    ap.add_argument("--intervalo", type=float, default=10.0)
    ap.add_argument("--limite", type=int, default=0)
    ap.add_argument("--espera", type=float, default=45.0,
                    help="segundos esperando o token; da tempo de resolver na janela")
    ap.add_argument("--refazer", action="store_true")
    args = ap.parse_args()

    registros = [r for r in ler_entrada(args.entrada) if r["data"]]
    os.makedirs(args.saida, exist_ok=True)
    if not args.refazer:
        registros = [r for r in registros
                     if not os.path.exists(os.path.join(args.saida, "%s.pdf" % r["cpf"]))]
    if args.limite:
        registros = registros[:args.limite]
    if not registros:
        print("[i] Nada a consultar.")
        return

    relatorio = os.path.join(args.saida, "relatorio.csv")
    novo = not os.path.exists(relatorio)
    log = open(relatorio, "a", newline="", encoding="utf-8")
    escritor = csv.writer(log, delimiter=";")
    if novo:
        escritor.writerow(["cpf", "nome", "data_nascimento", "status", "detalhe"])

    print("[+] %d CPF na fila | perfil: %s" % (len(registros), PERFIL))
    print("[i] Mouse real: %s" % ("sim" if pyautogui_ok() else "nao (clique via CDP)"))
    contagem = {}

    with SB(uc=True, locale="pt-BR", binary_location=CHROME_BINARY, user_data_dir=PERFIL) as sb:
        sb.activate_cdp_mode(URL)
        sb.sleep(2)
        for i, registro in enumerate(registros, 1):
            print("\n[%d/%d] %s (%s / %s)" % (i, len(registros),
                  registro["nome"] or registro["cpf"], registro["cpf"], registro["data"]))
            try:
                status, detalhe = consultar(sb, registro, args.saida, args.espera)
            except Exception as e:
                status, detalhe = "erro", str(e)[:200]
            contagem[status] = contagem.get(status, 0) + 1
            print("    -> %s %s" % (status, detalhe or ""))
            escritor.writerow([registro["cpf"], registro["nome"], registro["data"], status, detalhe])
            log.flush()
            if i < len(registros):
                time.sleep(args.intervalo)

    log.close()
    print("\n" + "=" * 50)
    for status, qtd in sorted(contagem.items()):
        print("%-16s %d" % (status, qtd))
    print("relatorio: %s" % relatorio)


if __name__ == "__main__":
    main()
