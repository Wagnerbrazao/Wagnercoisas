"""Consulta CPF ou CNPJ na Receita Federal em um único script.

    venv/bin/python rodar_tudo.py CNPJ
    venv/bin/python rodar_tudo.py CPF
    venv/bin/python rodar_tudo.py CPF DD/MM/AAAA

CPF sem data consulta o nascimento no Projudi. Sem argumentos, usa CPF ou CNPJ do .env.
"""

import argparse
import collections
import csv
import json
import os
import random
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.request
import warnings
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore", category=FutureWarning)

import google.generativeai as genai
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

AQUI = Path(__file__).resolve().parent
load_dotenv(AQUI / ".env")

PORTA = 9222
CDP_URL = "http://127.0.0.1:%d" % PORTA
CHROME = "/opt/google/chrome/google-chrome"
PERFIL_REAL = Path.home() / ".config" / "google-chrome"
PERFIL_CDP = AQUI.parent / "chrome_profile_enzo"
SAIDA_PADRAO = AQUI / "output" / "playwright"
FUSO = ZoneInfo("America/Sao_Paulo")
URLS = {
    "cpf": "https://servicos.receita.fazenda.gov.br/Servicos/CPF/ConsultaSituacao/ConsultaPublica.asp",
    "cnpj": "https://solucoes.receita.fazenda.gov.br/Servicos/cnpjreva/",
}
PERGUNTA_CAPTCHA = (
    "This is an hCaptcha image challenge with a 3x3 grid, numbered 1 to 9 "
    "left to right, top to bottom. Read the instruction in the image and "
    "compare the reference image with the grid. Reply ONLY with the numbers "
    "of the matching cells, separated by commas. Example: 2,5,8."
)


def normalizar_documento(valor):
    return re.sub(r"[.\-/\s]", "", valor or "").upper()


def identificar_consulta(documento, data=None):
    documento = normalizar_documento(documento)
    if re.fullmatch(r"\d{11}", documento):
        if not data:
            return "cpf", documento, None
        for formato in ("%d/%m/%Y", "%d%m%Y", "%Y-%m-%d", "%d-%m-%Y"):
            try:
                nascimento = datetime.strptime(data.strip(), formato).date()
                break
            except ValueError:
                continue
        else:
            raise ValueError("Data de nascimento inválida. Use DD/MM/AAAA.")
        if nascimento > datetime.now(FUSO).date():
            raise ValueError("A data de nascimento não pode estar no futuro.")
        return "cpf", documento, nascimento.strftime("%d/%m/%Y")
    if re.fullmatch(r"[A-Z0-9]{12}\d{2}", documento):
        if data:
            raise ValueError("A consulta de CNPJ recebe somente o documento, sem data de nascimento.")
        return "cnpj", documento, None
    raise ValueError("Informe um CPF com 11 dígitos ou um CNPJ com 14 caracteres.")


def tempo_restante(prazo, limite=30):
    restante = prazo - time.monotonic()
    if restante <= 0:
        raise TimeoutError("Tempo máximo da consulta atingido.")
    return max(1, int(min(restante, limite) * 1000))


def encerrar_consulta_projudi(processo):
    """Encerra somente os processos criados para esta consulta, se ainda ativos."""
    if processo.poll() is not None:
        return
    try:
        os.killpg(processo.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        processo.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(processo.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        processo.communicate()


def consultar_nascimento_projudi(documento, prazo):
    script = Path(os.environ.get("PROJUDI_NASCIMENTO_SCRIPT") or (
        AQUI.parent / "consulta_nascimento_projudi.py"
    )).expanduser().resolve()
    projeto = Path(os.environ.get("PROJUDI_ROBO_DIR") or (
        script.parent.parent / "CEAJE_PROTOCOLACAO_ROBO"
    )).expanduser().resolve()
    python = Path(os.environ.get("PROJUDI_PYTHON") or (
        projeto / ".venv" / "bin" / "python"
    )).expanduser().absolute()
    if not script.is_file():
        raise RuntimeError("Script de nascimento não encontrado. Configure PROJUDI_NASCIMENTO_SCRIPT.")
    if not (projeto / "script.projudi.py").is_file():
        raise RuntimeError("Robô do Projudi não encontrado. Configure PROJUDI_ROBO_DIR.")
    if not python.is_file():
        raise RuntimeError("Python do robô do Projudi não encontrado. Configure PROJUDI_PYTHON.")

    with tempfile.TemporaryDirectory(prefix="consulta-nascimento-") as temporario:
        pasta = Path(temporario)
        entrada, saida = pasta / "entrada.csv", pasta / "saida.csv"
        with entrada.open("w", encoding="utf-8", newline="") as arquivo:
            escritor = csv.writer(arquivo, delimiter=";")
            escritor.writerow(["cpf"])
            escritor.writerow([documento])
        ambiente = os.environ.copy()
        ambiente["PROJUDI_ROBO_DIR"] = str(projeto)
        ambiente["PROJUDI_NAO_INTERATIVO"] = "1"
        comando = [
            str(python), "-B", str(script), str(entrada), str(saida),
            "--coluna", "cpf", "--limite", "1", "--sem-manter-navegador",
            "--user-data-dir", str(pasta / "perfil"), "--nao-confirmar-passo3",
        ]
        restante = tempo_restante(prazo, limite=float("inf")) / 1000
        processo = subprocess.Popen(
            comando, cwd=str(script.parent), env=ambiente, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", start_new_session=True,
        )
        try:
            registros, _ = processo.communicate(timeout=restante)
        except subprocess.TimeoutExpired:
            raise TimeoutError("Tempo máximo atingido durante a consulta de nascimento no Projudi.") from None
        finally:
            encerrar_consulta_projudi(processo)
        if processo.returncode != 0:
            if "segundo fator" in registros.lower() or "tela de login" in registros.lower():
                raise RuntimeError("O Projudi não concluiu a autenticação. Verifique o login e o segundo fator do robô.")
            raise RuntimeError("O script de nascimento do Projudi terminou com erro (%d)." % processo.returncode)
        if not saida.is_file():
            raise RuntimeError("A consulta de nascimento do Projudi não gerou resultado.")
        with saida.open(encoding="utf-8-sig", newline="") as arquivo:
            linhas = list(csv.DictReader(arquivo, delimiter=";"))
        if len(linhas) != 1 or normalizar_documento(linhas[0].get("cpf")) != documento:
            raise RuntimeError("O resultado do Projudi não corresponde ao CPF solicitado.")
        linha = linhas[0]
        situacao = (linha.get("situacao") or "").strip().lower()
        if situacao == "nao cadastrado":
            raise RuntimeError("CPF não cadastrado no Projudi; data de nascimento indisponível.")
        if situacao != "encontrado":
            raise RuntimeError("O Projudi não conseguiu consultar o cadastro deste CPF.")
        data = (linha.get("data_nascimento") or "").strip()
        if not data:
            raise RuntimeError("O cadastro do Projudi não contém data de nascimento para este CPF.")
        try:
            return identificar_consulta(documento, data)[2]
        except ValueError:
            raise RuntimeError("A data de nascimento retornada pelo Projudi é inválida.") from None


def cdp_ativo():
    try:
        with urllib.request.urlopen(CDP_URL + "/json/version", timeout=1) as resposta:
            return bool(json.load(resposta).get("webSocketDebuggerUrl"))
    except (OSError, ValueError):
        return False


def flags_janela():
    com_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    visivel = os.environ.get("ROBO_JANELA", "").strip().lower() in (
        "visivel", "visível", "sim", "1", "true"
    )
    if visivel and com_display:
        return ["--start-maximized"]
    flags = ["--ozone-platform=headless", "--window-size=1920,1080"]
    if not com_display:
        flags += ["--use-gl=angle", "--use-angle=swiftshader"]
    return flags


def garantir_chrome(prazo):
    if cdp_ativo():
        return
    destino = PERFIL_CDP / "Default"
    if not destino.is_dir():
        origem = PERFIL_REAL / "Profile 2"
        perfil_em_uso = os.path.lexists(PERFIL_REAL / "SingletonLock")
        PERFIL_CDP.mkdir(parents=True, exist_ok=True)
        if origem.is_dir() and not perfil_em_uso:
            shutil.copytree(origem, destino, ignore=shutil.ignore_patterns(
                "Singleton*", "*.pma", "Crashpad*", "component_crx_cache"
            ))
            if (PERFIL_REAL / "Local State").is_file():
                shutil.copyfile(PERFIL_REAL / "Local State", PERFIL_CDP / "Local State")
        else:
            destino.mkdir(parents=True, exist_ok=True)
    chrome = CHROME if Path(CHROME).is_file() else shutil.which("google-chrome")
    if not chrome:
        raise RuntimeError("Google Chrome não encontrado nesta máquina.")
    print("[+] Iniciando o Chrome da automação...", flush=True)
    processo = subprocess.Popen(
        [chrome, "--remote-debugging-port=%d" % PORTA,
         "--remote-allow-origins=http://127.0.0.1:%d" % PORTA,
         "--user-data-dir=%s" % PERFIL_CDP, "--profile-directory=Default",
         *flags_janela(), "--lang=pt-BR", "--no-first-run", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    limite = min(prazo, time.monotonic() + 30)
    while time.monotonic() < limite:
        if cdp_ativo():
            return
        if processo.poll() is not None:
            break
        time.sleep(0.25)
    raise RuntimeError("O Chrome da automação não iniciou na porta %d." % PORTA)


SITES_AQUECIMENTO = (
    "https://www.youtube.com/",
    "https://www.google.com/",
    "https://pt.wikipedia.org/wiki/Especial:Aleat%C3%B3ria",
    "https://g1.globo.com/",
    "https://www.uol.com.br/",
    "https://www.climatempo.com.br/",
    "https://www.bbc.com/portuguese",
    "https://www.mercadolivre.com.br/",
    "https://ge.globo.com/",
)


def passear(page, url, segundos):
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    fim = time.monotonic() + segundos
    while time.monotonic() < fim:
        sorteio = random.random()
        if sorteio < 0.6:
            page.mouse.wheel(0, random.randint(200, 700))
        elif sorteio < 0.8:
            page.mouse.wheel(0, -random.randint(100, 400))
        else:
            page.mouse.move(random.randint(100, 1500), random.randint(100, 800),
                            steps=random.randint(5, 15))
        time.sleep(random.uniform(1.5, 4.0))


def aquecer_perfil(quantos=3, por_site=(25, 55)):
    """O hCaptcha pontua o navegador pelo historico de uso: perfil que so abre
    a pagina da Receita, resolve captcha e sai cobra desafio mais dificil."""
    escolhidos = random.sample(SITES_AQUECIMENTO, min(quantos, len(SITES_AQUECIMENTO)))
    with sync_playwright() as p:
        navegador = p.chromium.connect_over_cdp(CDP_URL, timeout=30000)
        page = navegador.contexts[0].new_page()
        try:
            for url in escolhidos:
                segundos = random.uniform(*por_site)
                sitio = url.split("/")[2]
                print("[~] Aquecendo o perfil em %s (%ds)..." % (sitio, segundos), flush=True)
                try:
                    passear(page, url, segundos)
                except Exception as erro:
                    print("[!] %s nao respondeu: %s" % (sitio, mensagem_segura(erro)), flush=True)
        finally:
            page.close()


def modelo_gemini():
    chave = os.environ.get("GEMINI_API_KEY", "").strip()
    if not chave:
        raise ValueError("Configure GEMINI_API_KEY no .env.")
    genai.configure(api_key=chave)
    return genai.GenerativeModel(
        os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        generation_config={"temperature": 0, "max_output_tokens": 100},
    )


def perguntar(modelo, imagem, prazo):
    for tentativa in range(3):
        try:
            resposta = modelo.generate_content(
                [PERGUNTA_CAPTCHA, {"mime_type": "image/png", "data": imagem}],
                request_options={"timeout": tempo_restante(prazo) / 1000},
            )
            return sorted({int(n) for n in re.findall(r"\b[1-9]\b", resposta.text)})
        except Exception as erro:
            if not any(codigo in str(erro) for codigo in ("429", "503", "504")) or tentativa == 2:
                raise
            print("[!] Serviço de IA temporariamente indisponível; aguardando 10s.", flush=True)
            time.sleep(min(10, tempo_restante(prazo) / 1000))


def resultado_aberto(page, tipo):
    if tipo == "cpf":
        return "ConsultaPublicaExibir.asp" in page.url
    return page.url.rstrip("/").endswith("/cnpjreva/comprovante")


def captcha_pronto(page, tipo):
    if resultado_aberto(page, tipo):
        return True
    if tipo == "cnpj":
        return page.get_by_role("button", name=re.compile("Consultar", re.I)).is_enabled()
    return page.evaluate(
        "() => { const e = document.querySelector(\"textarea[name='h-captcha-response']\");"
        " return !!e && e.value.length > 100; }"
    )


def resolver_captcha(page, modelo, tipo, prazo):
    page.frame_locator(".h-captcha iframe").locator("#checkbox").click(
        timeout=tempo_restante(prazo)
    )
    for tentativa in range(1, 9):
        desafio = None
        for _ in range(24):
            tempo_restante(prazo)
            if captcha_pronto(page, tipo):
                print("[+] Captcha concluído.", flush=True)
                return
            for iframe in page.locator('iframe[src*="frame=challenge"]').all():
                if iframe.is_visible():
                    desafio = iframe
                    break
            if desafio is not None:
                break
            time.sleep(0.25)
        if desafio is None:
            continue
        frame = desafio.element_handle().content_frame()
        if frame is None:
            continue
        imagens = frame.locator(".task-image")
        try:
            imagens.first.wait_for(state="visible", timeout=tempo_restante(prazo, 8))
        except PlaywrightTimeout:
            continue
        if imagens.count() != 9:
            raise RuntimeError("O tipo de desafio apresentado não é uma grade de 9 imagens.")
        imagem = desafio.screenshot(timeout=tempo_restante(prazo, 10))
        numeros = perguntar(modelo, imagem, prazo)
        print("[i] Captcha: tentativa %d/8." % tentativa, flush=True)
        if not numeros:
            continue
        for numero in numeros:
            imagens.nth(numero - 1).click(timeout=tempo_restante(prazo, 5))
            time.sleep(0.15)
        frame.locator(".button-submit").first.click(timeout=tempo_restante(prazo, 5))
        for _ in range(16):
            tempo_restante(prazo)
            if captcha_pronto(page, tipo):
                print("[+] Captcha concluído.", flush=True)
                return
            time.sleep(0.25)
    raise RuntimeError("O captcha não foi concluído dentro das tentativas disponíveis.")


def preencher(page, tipo, documento, data, prazo):
    page.goto(URLS[tipo], wait_until="domcontentloaded", timeout=tempo_restante(prazo, 45))
    page.frame_locator(".h-captcha iframe").locator("#checkbox").wait_for(
        state="visible", timeout=tempo_restante(prazo)
    )
    if tipo == "cpf":
        page.locator("#txtCPF").fill(documento)
        page.locator("#txtDataNascimento").fill(data)
        page.locator("#txtDataNascimento").press("Tab")
        return
    campo = page.locator("input[mask]")
    campo.fill(documento)
    campo.press("Tab")
    page.wait_for_function(
        "cnpj => { const campo = document.querySelector('input[mask]');"
        " const limpar = document.querySelector('button[type=reset]');"
        " return campo && campo.value.replace(/[^A-Z0-9]/gi, '').toUpperCase() === cnpj"
        " && limpar && !limpar.disabled; }",
        arg=documento, timeout=tempo_restante(prazo, 10),
    )


def enviar_consulta(page, tipo, prazo):
    if resultado_aberto(page, tipo):
        return
    if tipo == "cpf":
        # O formulário legado exige o token também nestes campos próprios.
        page.evaluate("""() => {
            const token = document.querySelector("textarea[name='h-captcha-response']").value;
            const check = document.getElementsByName('idCheckedReCaptcha')[0];
            if (check) check.value = 'true';
            let resposta = document.getElementById('h-recaptcha-response');
            if (!resposta) {
                resposta = document.createElement('input');
                resposta.type = 'hidden';
                resposta.id = resposta.name = 'h-recaptcha-response';
                document.getElementById('theForm').appendChild(resposta);
            }
            resposta.value = token;
        }""")
        page.locator("#id_submit").click(timeout=tempo_restante(prazo, 10))
        destino = "**/ConsultaPublicaExibir.asp*"
    else:
        page.get_by_role("button", name=re.compile("Consultar", re.I)).click(
            timeout=tempo_restante(prazo, 10)
        )
        destino = "**/comprovante"
    try:
        page.wait_for_url(destino, timeout=tempo_restante(prazo, 30))
        page.wait_for_load_state("domcontentloaded", timeout=tempo_restante(prazo))
    except PlaywrightTimeout:
        mensagem = "O site não abriu a página do comprovante."
        if tipo == "cnpj":
            mensagem = page.locator("app-message-container").inner_text().strip() or mensagem
        raise RuntimeError(mensagem) from None


def texto_normalizado(texto):
    return "".join(c for c in unicodedata.normalize("NFD", texto.upper())
                   if unicodedata.category(c) != "Mn")


def extrair_resultado(page, tipo, documento):
    texto = page.inner_text("body")
    normalizado = texto_normalizado(texto)
    linhas = [linha.strip() for linha in texto.splitlines() if linha.strip()]

    def valor_apos(rotulo):
        for indice, linha in enumerate(linhas[:-1]):
            if texto_normalizado(linha) == rotulo:
                return linhas[indice + 1]
        return ""

    if tipo == "cnpj":
        inscricao = re.search(r"NUMERO DE INSCRICAO\s+([A-Z0-9./-]+)", normalizado)
        if not inscricao or normalizar_documento(inscricao.group(1)) != documento:
            raise RuntimeError("O comprovante não corresponde ao CNPJ consultado.")
        if "NOME EMPRESARIAL" not in normalizado or not re.search(
            r"COMPROVANTE DE INSCRICAO E (?:DE )?SITUACAO CADASTRAL", normalizado
        ):
            raise RuntimeError("A página recebida não contém o comprovante de CNPJ.")
        campos = {"nome_empresarial": valor_apos("NOME EMPRESARIAL"),
                  "situacao_cadastral": valor_apos("SITUACAO CADASTRAL")}
    else:
        encontrados = re.findall(r"\b(?:\d{3}\.\d{3}\.\d{3}-\d{2}|\d{11})\b", texto)
        situacao = re.search(r"Situa..o Cadastral\s*:\s*([^\n]+)", texto, re.I)
        if (documento not in {normalizar_documento(v) for v in encontrados}
                or "COMPROVANTE DE SITUACAO CADASTRAL NO CPF" not in normalizado
                or not situacao):
            raise RuntimeError("A página recebida não contém o comprovante do CPF consultado.")
        nome = re.search(r"\bNome\s*:\s*([^\n]+)", texto, re.I)
        campos = {"nome": nome.group(1).strip() if nome else "",
                  "situacao_cadastral": situacao.group(1).strip()}
    return {"tipo": tipo, tipo: documento, **campos, "url": page.url,
            "consultado_em": datetime.now(FUSO).isoformat(timespec="seconds"), "texto": texto}


def salvar_pdf(page, tipo, destino, prazo):
    if tipo == "cpf":
        estilo = page.add_style_tag(content="""@media print {
            [vw], #vlibras-container, #vlibras-button, #barra-brasil { display: none !important; }
            a[href]::after { content: none !important; }
            html, body { height: auto !important; min-height: 0 !important; margin: 0 !important; }
        }""")
        try:
            page.pdf(path=str(destino), format="A4", print_background=True)
        finally:
            estilo.evaluate("el => el.remove()")
        return
    with page.expect_popup(timeout=tempo_restante(prazo, 10)) as popup:
        page.get_by_role("button", name=re.compile("Imprimir", re.I)).click(
            timeout=tempo_restante(prazo, 10)
        )
    impressao = popup.value
    try:
        impressao.locator("#principal").wait_for(state="visible", timeout=tempo_restante(prazo, 15))
        impressao.wait_for_function(
            "() => document.fonts.status === 'loaded' && [...document.images].every(img => img.complete)",
            timeout=tempo_restante(prazo, 15),
        )
        impressao.pdf(path=str(destino), format="A4", print_background=True)
    finally:
        impressao.close()


def mensagem_segura(erro, documento="", data=None):
    mensagem = str(erro)
    ocultos = [valor for nome, valor in os.environ.items() if valor and (
        any(parte in nome.upper() for parte in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
        or nome in ("CPF", "DATA_NASCIMENTO"))]
    if re.fullmatch(r"\d{11}", documento):
        ocultos += [documento, "%s.%s.%s-%s" % (
            documento[:3], documento[3:6], documento[6:9], documento[9:])]
    if data:
        ocultos += [data, re.sub(r"\D", "", data)]
    for valor in sorted(set(ocultos), key=len, reverse=True):
        mensagem = mensagem.replace(valor, "[oculto]")
    return mensagem[:600]


def executar(documento, data=None, saida=None, tempo_max=180):
    documento = normalizar_documento(documento)
    pasta = None
    try:
        tipo, documento, data = identificar_consulta(documento, data)
        if tempo_max <= 0:
            raise ValueError("O tempo máximo deve ser maior que zero.")
        prazo = time.monotonic() + tempo_max
        modelo = modelo_gemini()
        fonte_nascimento = "informada" if data else None
        if tipo == "cpf" and not data:
            print("[+] Buscando data de nascimento no Projudi...", flush=True)
            data = consultar_nascimento_projudi(documento, prazo)
            fonte_nascimento = "projudi"
            print("[+] Data de nascimento obtida e validada.", flush=True)
        garantir_chrome(prazo)
        base = Path(saida).expanduser().resolve() if saida else SAIDA_PADRAO / tipo
        pasta = base / (documento + "_" + datetime.now(FUSO).strftime("%Y%m%d_%H%M%S_%f"))
        pasta.mkdir(parents=True, exist_ok=True)
        print("[1/3] Consulta de %s identificada. Abrindo uma nova aba..." % tipo.upper(), flush=True)
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(CDP_URL, timeout=tempo_restante(prazo))
            page = browser.contexts[0].new_page()
            try:
                preencher(page, tipo, documento, data, prazo)
                print("[2/3] Dados preenchidos. Resolvendo o hCaptcha...", flush=True)
                resolver_captcha(page, modelo, tipo, prazo)
                enviar_consulta(page, tipo, prazo)
                resultado = extrair_resultado(page, tipo, documento)
                if tipo == "cpf":
                    resultado.update({"data_nascimento": data, "fonte_data_nascimento": fonte_nascimento})
                print("[3/3] Comprovante confirmado. Salvando os arquivos...", flush=True)
                page.screenshot(path=str(pasta / "comprovante.png"), full_page=True)
                salvar_pdf(page, tipo, pasta / "comprovante.pdf", prazo)
                resultado.update({"status": "ok", "pdf": str(pasta / "comprovante.pdf"),
                                  "imagem": str(pasta / "comprovante.png")})
                (pasta / "resultado.json").write_text(
                    json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception:
                if browser.is_connected() and not page.is_closed():
                    try:
                        page.screenshot(path=str(pasta / "erro.png"), full_page=True, timeout=5000)
                    except Exception:
                        pass
                raise
        print("SUCESSO: COMPROVANTE DE %s CONFIRMADO" % tipo.upper(), flush=True)
        print("Arquivos: %s" % (pasta if tipo == "cnpj" else pasta.parent), flush=True)
        print("[i] O Chrome e as outras abas continuam abertos.", flush=True)
        return 0
    except Exception as erro:
        mensagem = mensagem_segura(erro, documento, data)
        print("[-] Consulta falhou: %s" % mensagem, file=sys.stderr, flush=True)
        if pasta is not None:
            (pasta / "resultado.json").write_text(
                json.dumps({"status": "erro", "mensagem": mensagem}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return 1


def ler_fila(caminho):
    with open(caminho, newline="", encoding="utf-8-sig") as arquivo:
        amostra = arquivo.read(4096)
        arquivo.seek(0)
        try:
            dialeto = csv.Sniffer().sniff(amostra, delimiters=";,\t")
        except csv.Error:
            dialeto = csv.excel
            dialeto.delimiter = ";"
        leitor = csv.DictReader(arquivo, dialect=dialeto)
        colunas = {(nome or "").strip().lower(): nome for nome in (leitor.fieldnames or [])}
        col_doc = (colunas.get("cpf") or colunas.get("cnpj") or colunas.get("documento")
                   or colunas.get("documento_credito"))
        col_data = colunas.get("data_nascimento") or colunas.get("nascimento") or colunas.get("data")
        col_nome = colunas.get("nome") or colunas.get("nome_credito")
        if not col_doc:
            raise ValueError("O CSV precisa de uma coluna 'cpf', 'cnpj' ou 'documento'.")
        fila, vistos, ignorados = [], set(), 0
        for linha in leitor:
            documento = normalizar_documento(linha.get(col_doc, ""))
            data = (linha.get(col_data) or "").strip() if col_data else ""
            if not documento or documento in vistos:
                continue
            try:
                identificar_consulta(documento, data or None)
            except ValueError:
                try:
                    identificar_consulta(documento)  # CNPJ com data sobrando no CSV
                except ValueError:
                    ignorados += 1
                    continue
                data = ""
            vistos.add(documento)
            fila.append({"documento": documento, "data": data or None,
                         "nome": (linha.get(col_nome) or "").strip() if col_nome else ""})
        return fila, ignorados


def ja_consultado(documento, saida=None):
    if saida:
        bases = [Path(saida).expanduser().resolve()]
    else:
        bases = [SAIDA_PADRAO / "cpf", SAIDA_PADRAO / "cnpj"]
    return any(pdf.is_file() for base in bases
               for pdf in base.glob(documento + "_*/comprovante.pdf"))


def executar_lote(entrada, saida=None, tempo_max=180, repescagens=1, intervalo=10, aquecer=True):
    try:
        fila, ignorados = ler_fila(entrada)
    except (OSError, ValueError) as erro:
        print("[-] Nao deu para ler o CSV: %s" % mensagem_segura(erro), file=sys.stderr, flush=True)
        return 1
    if ignorados:
        print("[!] %d linha(s) sem documento ou data valida foram ignoradas." % ignorados, flush=True)
    pendentes = collections.deque(r for r in fila if not ja_consultado(r["documento"], saida))
    prontos = len(fila) - len(pendentes)
    print("[+] %d documento(s) no CSV | %d ja consultados | %d na fila"
          % (len(fila), prontos, len(pendentes)), flush=True)
    if not pendentes:
        return 0

    if aquecer:
        try:
            garantir_chrome(time.monotonic() + 60)
            aquecer_perfil()
        except Exception as erro:
            print("[!] Nao deu para aquecer o perfil: %s" % mensagem_segura(erro), flush=True)

    base = Path(saida).expanduser().resolve() if saida else SAIDA_PADRAO
    base.mkdir(parents=True, exist_ok=True)
    relatorio = base / "relatorio.csv"
    novo = not relatorio.exists()
    contagem, voltas = {}, {}
    with open(relatorio, "a", newline="", encoding="utf-8") as arquivo:
        escritor = csv.writer(arquivo, delimiter=";")
        if novo:
            escritor.writerow(["quando", "documento", "nome", "data", "status"])
        posicao, total = 0, len(pendentes)
        while pendentes:
            registro = pendentes.popleft()
            documento = registro["documento"]
            posicao += 1
            print("\n[%d/%d] %s %s" % (posicao, total, documento, registro["nome"]), flush=True)
            codigo = executar(documento, registro["data"], saida, tempo_max)
            if codigo and voltas.get(documento, 0) < repescagens:
                voltas[documento] = voltas.get(documento, 0) + 1
                pendentes.append(registro)
                total += 1
                status = "repescagem"
                print("[i] Volta para o fim da fila (%d na frente)." % (len(pendentes) - 1), flush=True)
            else:
                status = "ok" if codigo == 0 else "falhou"
            contagem[status] = contagem.get(status, 0) + 1
            escritor.writerow([datetime.now(FUSO).strftime("%d/%m/%Y %H:%M:%S"), documento,
                               registro["nome"], registro["data"] or "", status])
            arquivo.flush()
            if pendentes and intervalo > 0:
                time.sleep(intervalo)

    print("\n" + "=" * 50, flush=True)
    for status in sorted(contagem):
        print("%-14s %d" % (status, contagem[status]), flush=True)
    print("relatorio: %s" % relatorio, flush=True)
    return 0 if not contagem.get("falhou") else 1


def ler_argumentos(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("documento", nargs="?", help="CPF ou CNPJ, com ou sem pontuação")
    parser.add_argument("nascimento", nargs="?", help="Data opcional para CPF; sem ela, consulta o Projudi")
    grupo = parser.add_mutually_exclusive_group()
    grupo.add_argument("--cnpj", nargs="?", const="", help="CNPJ ou variável CNPJ do .env")
    grupo.add_argument("--cpf", nargs="?", const="", help="CPF ou variável CPF do .env")
    parser.add_argument("--data", "--data-nascimento", dest="data", help="Data opcional para CPF (DD/MM/AAAA)")
    parser.add_argument("--saida", type=Path, help="Pasta dos comprovantes")
    parser.add_argument("--tempo-max", type=int, default=180, help="Tempo máximo em segundos (padrão: 180)")
    parser.add_argument("--entrada", type=Path,
                        help="CSV com uma coluna cpf/cnpj (e data_nascimento, nome) para consultar em lote")
    parser.add_argument("--repescagens", type=int, default=1,
                        help="Quantas vezes um documento volta para o fim da fila (padrão: 1)")
    parser.add_argument("--intervalo", type=float, default=10,
                        help="Pausa em segundos entre as consultas do lote (padrão: 10)")
    parser.add_argument("--sem-aquecer", dest="sem_aquecer", action="store_true",
                        help="Começa o lote sem navegar antes em sites comuns")
    parser.add_argument("--aquecer", nargs="?", const=3, type=int, metavar="SITES",
                        help="Só aquece o perfil: navega em N sites comuns e encerra (padrão: 3)")
    args = parser.parse_args(argv)
    if args.tempo_max <= 0:
        parser.error("--tempo-max deve ser maior que zero")
    if args.aquecer is not None:
        if args.documento or args.entrada or args.cpf is not None or args.cnpj is not None:
            parser.error("--aquecer só aquece o perfil; não informe documento nem --entrada.")
        if args.aquecer < 1:
            parser.error("--aquecer precisa de pelo menos um site")
        return {"quantos": args.aquecer}
    if args.entrada is not None:
        if args.documento or args.cpf is not None or args.cnpj is not None or args.nascimento or args.data:
            parser.error("--entrada consulta o CSV inteiro; não informe documento nem data.")
        if args.repescagens < 0:
            parser.error("--repescagens não pode ser negativo")
        if args.intervalo < 0:
            parser.error("--intervalo não pode ser negativo")
        if not args.entrada.is_file():
            parser.error("Arquivo de entrada não encontrado: %s" % args.entrada)
        return {"entrada": args.entrada, "saida": args.saida, "tempo_max": args.tempo_max,
                "repescagens": args.repescagens, "intervalo": args.intervalo,
                "aquecer": not args.sem_aquecer}
    if args.documento is not None and (args.cnpj is not None or args.cpf is not None):
        parser.error("Informe o documento uma única vez, como argumento ou opção.")
    if args.nascimento and args.data:
        parser.error("Informe a data de nascimento uma única vez.")
    documento, data = args.documento, args.nascimento or args.data
    esperado = None
    if args.cnpj is not None:
        documento, esperado = args.cnpj or os.environ.get("CNPJ", ""), "cnpj"
    elif args.cpf is not None:
        documento, esperado = args.cpf or os.environ.get("CPF", ""), "cpf"
    elif documento is None:
        documento = os.environ.get("CPF", "") or os.environ.get("CNPJ", "")
    try:
        tipo, documento, data = identificar_consulta(documento, data)
        if esperado and tipo != esperado:
            raise ValueError("O documento informado não é um %s." % esperado.upper())
    except ValueError as erro:
        parser.error(str(erro))
    return {"documento": documento, "data": data, "saida": args.saida,
            "tempo_max": args.tempo_max}


def aquecer_somente(quantos=3, por_site=(40, 90)):
    try:
        garantir_chrome(time.monotonic() + 60)
        aquecer_perfil(quantos, por_site)
    except Exception as erro:
        print("[-] Aquecimento falhou: %s" % mensagem_segura(erro), file=sys.stderr, flush=True)
        return 1
    print("[+] Perfil aquecido: %s" % PERFIL_CDP, flush=True)
    return 0


def main(argv=None):
    opcoes = ler_argumentos(argv)
    if "quantos" in opcoes:
        return aquecer_somente(**opcoes)
    if "entrada" in opcoes:
        return executar_lote(**opcoes)
    return executar(**opcoes)


if __name__ == "__main__":
    sys.exit(main())
