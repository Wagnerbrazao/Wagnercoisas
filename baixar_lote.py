"""
Baixa em lote o "Comprovante de Situacao Cadastral no CPF" (Receita Federal)
para uma lista de CPF + data de nascimento, salvando um PDF por credor.

Entrada: CSV com as colunas  cpf;data_nascimento;nome  (cabecalho obrigatorio).
Saida:   um PDF por CPF em --saida  +  relatorio.csv com o status de cada um.

Uso:
    venv/bin/python baixar_lote.py --entrada /home/wagner/Downloads/carga_csv/cpfs_para_consulta.csv \
                                   --saida  /home/wagner/Downloads/carga_csv/comprovantes

Retoma de onde parou: CPF que ja tem PDF na pasta de saida e pulado.
"""

import argparse
import base64
import collections
import csv
import os
import random
import re
import sys
import time
import urllib.request
import warnings

warnings.filterwarnings("ignore")

import google.generativeai as genai
from playwright.sync_api import sync_playwright

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)

from shot_groq import parse_captcha_answer, perguntar_gemini  # noqa: E402

URL = "https://servicos.receita.fazenda.gov.br/Servicos/CPF/ConsultaSituacao/ConsultaPublica.asp"
CDP_URL = "http://127.0.0.1:9222"

PERGUNTA_CAPTCHA = (
    "You are a captcha solver. This is a 3x3 grid of images (numbered 1-9, "
    "left-to-right, top-to-bottom). There is a reference image at the top. Look at "
    "the instruction text in Portuguese. Identify which grid cells match the "
    "reference. Reply with ONLY the matching cell numbers as a comma-separated "
    "list. Nothing else. Example answer: 2,5,8"
)

HUMANISE = None
try:
    from movimento_humano import humanise_mouse_playwright as HUMANISE
except Exception:
    pass


# ---------------------------------------------------------------- entrada ---

def normaliza_cpf(valor):
    digitos = re.sub(r"\D", "", valor or "")
    return digitos.zfill(11) if 0 < len(digitos) <= 11 else digitos


def normaliza_data(valor):
    """Aceita dd/mm/aaaa, dd-mm-aaaa, ddmmaaaa ou aaaa-mm-dd. Devolve dd/mm/aaaa."""
    bruto = (valor or "").strip()
    if not bruto:
        return ""
    iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", bruto)
    if iso:
        return "%s/%s/%s" % (iso.group(3), iso.group(2), iso.group(1))
    digitos = re.sub(r"\D", "", bruto)
    if len(digitos) == 8:
        return "%s/%s/%s" % (digitos[:2], digitos[2:4], digitos[4:])
    return bruto


def ler_entrada(caminho):
    with open(caminho, newline="", encoding="utf-8-sig") as fh:
        primeira = fh.readline()
        if primeira.lower().startswith("sep="):
            inicio = fh.tell()
        else:
            fh.seek(0)
            inicio = 0
        amostra = fh.read(4096)
        fh.seek(inicio)
        try:
            dialeto = csv.Sniffer().sniff(amostra, delimiters=";,\t")
        except csv.Error:
            dialeto = csv.excel
            dialeto.delimiter = ";"
        leitor = csv.DictReader(fh, dialect=dialeto)
        campos = {(c or "").strip().lower(): c for c in (leitor.fieldnames or [])}
        col_cpf = campos.get("cpf") or campos.get("documento_credito")
        col_data = campos.get("data_nascimento") or campos.get("nascimento") or campos.get("data")
        col_nome = campos.get("nome") or campos.get("nome_credito")
        if not col_cpf:
            raise SystemExit("[-] O CSV precisa de uma coluna 'cpf'.")
        linhas, vistos = [], set()
        for reg in leitor:
            cpf = normaliza_cpf(reg.get(col_cpf, ""))
            if len(cpf) != 11 or cpf in vistos:
                continue
            vistos.add(cpf)
            linhas.append({
                "cpf": cpf,
                "data": normaliza_data(reg.get(col_data, "")) if col_data else "",
                "nome": (reg.get(col_nome, "") or "").strip() if col_nome else "",
            })
        return linhas


# ---------------------------------------------------------------- consulta ---

# "latest" tem cota diaria propria, separada dos modelos com versao no nome:
# quando 3.5/3.1-flash-lite esgotam, esses ainda respondem no mesmo dia.
MODELOS_PADRAO = ["gemini-flash-lite-latest", "gemini-3.5-flash-lite",
                  "gemini-3.1-flash-lite", "gemini-flash-latest",
                  "gemini-3-flash-preview"]

# modelo que raciocina antes de responder precisa de folga de tokens,
# senao o texto vem vazio (o orcamento inteiro vai para o raciocinio)
TOKENS_POR_MODELO = {"gemini-flash-latest": 400, "gemini-3-flash-preview": 400}


class SemModelos(Exception):
    """Acabaram as combinacoes de chave x modelo disponiveis hoje."""


class Rotador(object):
    """Troca de CHAVE primeiro e de modelo depois quando a cota diaria acaba.

    A cota do free tier e por projeto e por modelo, entao a mesma pergunta que
    esgotou numa chave volta a funcionar na outra.
    """

    def __init__(self, nomes, chaves):
        self.pares = [(m, k) for m in nomes for k in chaves]
        self.rotulos = ["%s/chave%d" % (m, chaves.index(k) + 1) for m, k in self.pares]
        self.i = 0
        self._model = None

    @property
    def atual(self):
        return self.rotulos[self.i] if self.i < len(self.pares) else None

    def _instancia(self):
        if self._model is None:
            modelo, chave = self.pares[self.i]
            genai.configure(api_key=chave)
            self._model = genai.GenerativeModel(
                modelo,
                generation_config={"temperature": 0,
                                   "max_output_tokens": TOKENS_POR_MODELO.get(modelo, 30)})
        return self._model

    def _proximo(self, motivo):
        gasto = self.atual
        self.i += 1
        self._model = None
        if self.atual is None:
            raise SemModelos("sem cota em todas as combinacoes: %s" % ", ".join(self.rotulos))
        print("    [!] %s indisponivel (%s). Trocando para %s." % (gasto, motivo, self.atual))

    def perguntar(self, imagem, pergunta):
        for _ in range(len(self.pares) * 2):
            try:
                return perguntar_gemini(self._instancia(), imagem, pergunta)
            except Exception as e:
                texto = str(e)
                if "503" in texto or "UNAVAILABLE" in texto:
                    self._proximo("503")
                    continue
                if "429" not in texto:
                    print("    [-] Erro Gemini (%s): %s" % (self.atual, texto[:120]))
                    return None
                if "PerDay" in texto or "per_day" in texto or "free_tier_requests" in texto:
                    self._proximo("cota diaria")
                    continue
                print("    [!] Limite por minuto em %s; aguardando 12s..." % self.atual)
                time.sleep(12)
        return None


def resolver_captcha(page, model, tentativas=8, prazo=None):
    """Clica no hCaptcha e resolve o desafio com o Gemini. True se passou."""
    caixa = page.locator(".h-captcha iframe").bounding_box()
    if caixa:
        x = caixa["x"] + 32
        y = caixa["y"] + caixa["height"] / 2
        page.mouse.move(x, y, steps=8)
        time.sleep(0.2)
        page.mouse.click(x, y)
    time.sleep(2)

    for tentativa in range(tentativas):
        if prazo and time.time() > prazo:
            print("    [!] Tempo maximo estourado; abandonando este CPF.")
            return False
        if "ConsultaPublicaExibir" in page.url:
            return True

        desafio = None
        for frame in page.frames:
            if "hcaptcha" in frame.url and "challenge" in frame.url:
                desafio = frame
                break
        if desafio is None:
            return True  # checkbox passou direto

        time.sleep(0.8)
        try:
            iframe = page.locator('iframe[title*="Desafio"]')
            if iframe.count() > 0 and iframe.is_visible():
                iframe.screenshot(path="/tmp/lote_challenge.png", timeout=5000)
            else:
                page.screenshot(path="/tmp/lote_challenge.png")
        except Exception:
            page.screenshot(path="/tmp/lote_challenge.png")

        resposta = model.perguntar("/tmp/lote_challenge.png", PERGUNTA_CAPTCHA)
        if not resposta:
            continue

        numeros = parse_captcha_answer(resposta)
        print("    [i] Tentativa %d -> %s" % (tentativa + 1, numeros or "sem numeros"))
        if not numeros:
            continue

        for frame in page.frames:
            if "hcaptcha" in frame.url and "challenge" in frame.url:
                desafio = frame
                break

        imagens = desafio.locator(".task-image")
        total = imagens.count()
        for num in numeros:
            idx = num - 1
            if not (0 <= idx < total):
                continue
            try:
                cx_box = imagens.nth(idx).bounding_box()
                if cx_box:
                    page.mouse.move(cx_box["x"] + cx_box["width"] / 2,
                                    cx_box["y"] + cx_box["height"] / 2, steps=6)
                    time.sleep(0.1)
                    page.mouse.click(cx_box["x"] + cx_box["width"] / 2,
                                     cx_box["y"] + cx_box["height"] / 2)
                    time.sleep(0.2)
            except Exception:
                pass

        time.sleep(0.3)
        try:
            enviar = desafio.locator("button.button-submit, [role=button].button-submit, "
                                     "div.button-submit, a.button-submit")
            if enviar.count() > 0:
                enviar.first.click(timeout=3000)
        except Exception:
            pass
        time.sleep(2)

        if "ConsultaPublicaExibir" in page.url:
            return True
        token = page.evaluate(
            "() => { const e = document.querySelector(\"textarea[name='h-captcha-response']\");"
            " return e ? e.value : ''; }"
        )
        if token and len(token) > 100:
            return True
    return False


def salvar_pdf(cdp, caminho):
    resultado = cdp.send("Page.printToPDF", {
        "printBackground": True,
        "marginTop": 0.4, "marginBottom": 0.4,
        "marginLeft": 0.4, "marginRight": 0.4,
    })
    with open(caminho, "wb") as fh:
        fh.write(base64.b64decode(resultado["data"]))


def mensagem_erro(page):
    try:
        texto = page.inner_text("body")[:4000]
    except Exception:
        return ""
    for padrao in ("Data de nascimento informada", "CPF informado", "não existe",
                   "nao existe", "Erro no preenchimento", "sistema encontra-se"):
        if padrao.lower() in texto.lower():
            linha = [l.strip() for l in texto.splitlines()
                     if padrao.lower() in l.lower() and l.strip()]
            if linha:
                return linha[0][:200]
    return ""


def ja_baixado(saida, cpf):
    """True se ja existe comprovante desse CPF, com o nome antigo ou o novo."""
    try:
        return any(n.startswith(cpf) and n.endswith(".pdf") for n in os.listdir(saida))
    except FileNotFoundError:
        return False


def nome_com_situacao(saida, cpf, corpo):
    """Caminho "CPF - SITUACAO CADASTRAL.pdf" lido da pagina do comprovante."""
    m = re.search(r"Situa\S+o Cadastral:\s*(.+)", corpo or "")
    sit = re.sub(r"[^A-Za-z\u00c0-\u00ff ]+", "", m.group(1)).strip() if m else ""
    return os.path.join(saida, ("%s - %s.pdf" % (cpf, sit)) if sit else ("%s.pdf" % cpf))


def consultar(page, cdp, model, registro, saida, tentativas=8, tempo_max=0):
    cpf, data = registro["cpf"], registro["data"]
    destino = os.path.join(saida, "%s.pdf" % cpf)

    page.bring_to_front()
    page.goto(URL, wait_until="domcontentloaded")
    time.sleep(0.8)
    if HUMANISE:
        try:
            HUMANISE(page)
        except Exception:
            pass

    page.fill("#txtCPF", cpf)
    page.evaluate("() => document.getElementById('txtCPF').blur()")
    page.fill("#txtDataNascimento", data)
    page.evaluate("() => document.getElementById('txtDataNascimento').blur()")
    time.sleep(0.3)

    prazo = (time.time() + tempo_max) if tempo_max else None
    resolver_captcha(page, model, tentativas, prazo)

    if "ConsultaPublicaExibir" not in page.url:
        token = page.evaluate(
            "() => { const e = document.querySelector(\"textarea[name='h-captcha-response']\");"
            " return e ? e.value : ''; }"
        )
        if token:
            page.evaluate(r"""(t) => {
                var ta = document.querySelector("textarea[name='h-captcha-response']");
                if (ta) ta.value = t;
                var chk = document.getElementsByName("idCheckedReCaptcha")[0];
                if (chk) chk.value = "true";
                var a = document.getElementById("h-recaptcha-response");
                if (!a) {
                    a = document.createElement("input");
                    a.type = "hidden"; a.id = "h-recaptcha-response";
                    a.name = "h-recaptcha-response";
                    (document.getElementById("theForm") || document.body).appendChild(a);
                }
                a.value = t;
            }""", token)
        page.keyboard.press("Escape")
        time.sleep(0.3)
        try:
            page.click("#id_submit", force=True)
        except Exception:
            pass
        time.sleep(2.5)

    if "ConsultaPublicaExibir" in page.url:
        # a pagina de erro tambem cai nessa URL: sem o campo "Cadastral:" nao e
        # comprovante, e salvar isso como PDF passa erro por sucesso
        try:
            corpo = page.inner_text("body")
        except Exception:
            corpo = ""
        if "Cadastral:" not in corpo:
            aviso = ""
            for linha in corpo.splitlines():
                if "divergente" in linha.lower() or "nao existe" in linha.lower():
                    aviso = linha.strip()[:160]
                    break
            return "dados_invalidos", aviso or "pagina sem os campos do comprovante"
        destino = nome_com_situacao(saida, cpf, corpo)
        salvar_pdf(cdp, destino)
        return "ok", destino

    erro = mensagem_erro(page)
    return ("dados_invalidos" if erro else "captcha_falhou"), erro


# ------------------------------------------------------------------- main ---

def main():
    ap = argparse.ArgumentParser(description="Baixa comprovantes de situacao cadastral em lote.")
    ap.add_argument("--entrada", required=True, help="CSV com cpf;data_nascimento;nome")
    ap.add_argument("--saida", default=os.path.join(AQUI, "comprovantes"), help="pasta dos PDFs")
    ap.add_argument("--intervalo", type=float, default=12.0,
                    help="pausa media entre consultas, em segundos (padrao 12)")
    ap.add_argument("--limite", type=int, default=0, help="processa apenas os N primeiros")
    ap.add_argument("--refazer", action="store_true", help="reconsulta CPF que ja tem PDF")
    ap.add_argument("--tempo-max", type=float, default=180, dest="tempo_max",
                    help="segundos por CPF; ao estourar vai para o fim da fila (0 = sem limite)")
    ap.add_argument("--repescagens", type=int, default=1,
                    help="quantas vezes um CPF pode voltar para o fim da fila (padrao 1)")
    ap.add_argument("--tentativas", type=int, default=8,
                    help="tentativas de captcha por CPF (padrao 8)")
    args = ap.parse_args()

    chave = os.environ.get("GEMINI_API_KEY", "")
    if not chave:
        raise SystemExit("[-] Defina GEMINI_API_KEY (ou carregue o .env).")

    try:
        urllib.request.urlopen(CDP_URL + "/json/version", timeout=2)
    except Exception:
        raise SystemExit("[-] Chrome sem CDP. Rode antes: venv/bin/python iniciar_chrome.py")

    registros = ler_entrada(args.entrada)
    sem_data = [r for r in registros if not r["data"]]
    if sem_data:
        print("[!] %d de %d registros estao SEM data de nascimento e serao pulados."
              % (len(sem_data), len(registros)))
    fila = [r for r in registros if r["data"]]

    os.makedirs(args.saida, exist_ok=True)
    if not args.refazer:
        fila = [r for r in fila if not ja_baixado(args.saida, r["cpf"])]
    if args.limite:
        fila = fila[:args.limite]

    if not fila:
        print("[i] Nada a consultar (sem data de nascimento ou tudo ja baixado).")
        return

    modelos = [m.strip() for m in
               os.environ.get("GEMINI_MODELOS", ",".join(MODELOS_PADRAO)).split(",") if m.strip()]
    chaves = [chave]
    for extra in ("GEMINI_API_KEY_2", "GEMINI_API_KEY_3", "GEMINI_API_KEY_4"):
        valor = os.environ.get(extra, "").strip()
        if valor and valor not in chaves:
            chaves.append(valor)
    # GEMINI_CHAVE_PRIMEIRA=2 comeca pela chave 2 (util quando a 1 ja gastou hoje)
    primeira = os.environ.get("GEMINI_CHAVE_PRIMEIRA", "").strip()
    if primeira.isdigit():
        i = int(primeira) - 1
        if 0 <= i < len(chaves):
            chaves = chaves[i:] + chaves[:i]
    model = Rotador(modelos, chaves)
    print("[+] %d chave(s) x %d modelo(s): %s" % (len(chaves), len(modelos), " -> ".join(model.rotulos)))

    relatorio = os.path.join(args.saida, "relatorio.csv")
    novo = not os.path.exists(relatorio)
    log = open(relatorio, "a", newline="", encoding="utf-8")
    escritor = csv.writer(log, delimiter=";")
    if novo:
        escritor.writerow(["cpf", "nome", "data_nascimento", "status", "detalhe"])

    print("[+] %d CPF na fila. PDFs em: %s" % (len(fila), args.saida))
    contagem = {}

    with sync_playwright() as p:
        navegador = p.chromium.connect_over_cdp(CDP_URL)
        ctx = navegador.contexts[0]
        page = next((pg for pg in ctx.pages
                     if "servicos.receita.fazenda.gov.br" in pg.url), None)
        if page is None:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        cdp = ctx.new_cdp_session(page)

        pendentes = collections.deque(fila)
        voltas = {}
        seguidos_rede = 0
        i, total = 0, len(fila)
        while pendentes:
            registro = pendentes.popleft()
            cpf = registro["cpf"]
            i += 1
            marca = "" if not voltas.get(cpf) else " (repescagem %d)" % voltas[cpf]
            print("\n[%d/%d]%s %s (%s / %s)" % (i, total, marca,
                  registro["nome"] or cpf, cpf, registro["data"]))
            comeco = time.time()
            try:
                status, detalhe = consultar(page, cdp, model, registro, args.saida,
                                            args.tentativas, args.tempo_max)
            except SemModelos as e:
                print("\n[-] %s" % e)
                break
            except Exception as e:
                status, detalhe = "erro", str(e)[:200]
            contagem[status] = contagem.get(status, 0) + 1
            print("    -> %s (%.0fs) %s" % (status, time.time() - comeco, detalhe or ""))
            texto_erro = (detalhe or "") if status == "erro" else ""
            if "has been closed" in texto_erro:
                escritor.writerow([cpf, registro["nome"], registro["data"], status, detalhe])
                print("\n[-] Chrome fechou. Encerrando para o vigia subir de novo.")
                break
            seguidos_rede = (seguidos_rede + 1) if ("ERR_" in texto_erro or "Timeout" in texto_erro) else 0
            if seguidos_rede >= 5:
                escritor.writerow([cpf, registro["nome"], registro["data"], status, detalhe])
                print("\n[-] 5 falhas de rede seguidas. Encerrando para reautenticar a rede.")
                break
            escritor.writerow([cpf, registro["nome"], registro["data"], status, detalhe])
            log.flush()

            # nao saiu e ainda tem repescagem: volta para o fim da fila
            if status != "ok" and voltas.get(cpf, 0) < args.repescagens:
                voltas[cpf] = voltas.get(cpf, 0) + 1
                pendentes.append(registro)
                total += 1
                print("    [i] Volta para o fim da fila (%d na frente)." % (len(pendentes) - 1))

            if pendentes:
                pausa = max(3.0, random.uniform(args.intervalo * 0.7, args.intervalo * 1.3))
                time.sleep(pausa)

    log.close()
    print("\n" + "=" * 50)
    for status, qtd in sorted(contagem.items()):
        print("%-18s %d" % (status, qtd))
    print("relatorio: %s" % relatorio)


if __name__ == "__main__":
    main()
