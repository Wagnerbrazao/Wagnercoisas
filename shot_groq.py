"""
Usa Gemini vision para resolver hCaptcha automaticamente.
Preenche CPF, resolve o captcha via IA, e envia a consulta.
"""

import os
import re
import sys
import time
import urllib.request
import warnings

warnings.filterwarnings("ignore")

import google.generativeai as genai
from playwright.sync_api import sync_playwright

CPF = os.environ.get("CPF", "")
DATA_NASCIMENTO = os.environ.get("DATA_NASCIMENTO", "")
URL = "https://servicos.receita.fazenda.gov.br/Servicos/CPF/ConsultaSituacao/ConsultaPublica.asp"
CDP_URL = "http://127.0.0.1:9222"
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")

HUMANISE = None
try:
    from movimento_humano import humanise_mouse_playwright as HUMANISE
except Exception:
    pass


def perguntar_gemini(model, image_path, pergunta):
    with open(image_path, "rb") as f:
        img_bytes = f.read()
    resp = model.generate_content([
        pergunta,
        {"mime_type": "image/png", "data": img_bytes}
    ])
    return resp.text


def parse_captcha_answer(raw):
    nums = [int(n) for n in re.findall(r'\d+', raw) if 1 <= int(n) <= 9]
    return sorted(set(nums))


def main():
    if not GEMINI_KEY:
        print("[-] Defina GEMINI_API_KEY: export GEMINI_API_KEY=...")
        return

    try:
        urllib.request.urlopen(CDP_URL + "/json/version", timeout=1)
    except Exception:
        print("[-] Chrome sem CDP. Rode: venv/bin/python Captcha/iniciar_chrome.py")
        return

    genai.configure(api_key=GEMINI_KEY)
    model = genai.GenerativeModel(
        "gemini-3.5-flash-lite",
        # temperatura 0 = resposta determinística; poucos tokens = resposta mais rápida
        generation_config={"temperature": 0, "max_output_tokens": 30},
    )

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        ctx = browser.contexts[0]
        page = None
        for pg in ctx.pages:
            if "servicos.receita.fazenda.gov.br" in pg.url:
                page = pg
                break
        if page is None:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

        print("[+] Conectado.")
        page.bring_to_front()
        page.goto(URL, wait_until="domcontentloaded")
        time.sleep(0.8)

        if HUMANISE:
            HUMANISE(page)

        page.fill("#txtCPF", CPF)
        page.evaluate("() => document.getElementById('txtCPF').blur()")
        page.fill("#txtDataNascimento", DATA_NASCIMENTO)
        page.evaluate("() => document.getElementById('txtDataNascimento').blur()")
        time.sleep(0.3)
        print("[+] Preenchido.")

        bb = page.locator(".h-captcha iframe").bounding_box()
        if bb:
            x = bb["x"] + 32
            y = bb["y"] + bb["height"] / 2
            page.mouse.move(x, y, steps=8)
            time.sleep(0.2)
            page.mouse.click(x, y)
        time.sleep(2)

        resolved = False
        for tentativa in range(5):
            if "ConsultaPublicaExibir" in page.url:
                print("[+] Ja na pagina de resultado!")
                resolved = True
                break

            has_challenge = False
            for frame in page.frames:
                if "hcaptcha" in frame.url and "challenge" in frame.url:
                    has_challenge = True
                    break

            if not has_challenge:
                print("[+] Sem desafio ou ja resolvido!")
                break

            time.sleep(0.8)

            if "ConsultaPublicaExibir" in page.url:
                resolved = True
                break

            try:
                challenge_iframe = page.locator('iframe[title*="Desafio"]')
                if challenge_iframe.count() > 0 and challenge_iframe.is_visible():
                    challenge_iframe.screenshot(path="/tmp/gemini_challenge.png", timeout=5000)
                else:
                    page.screenshot(path="/tmp/gemini_challenge.png")
            except Exception:
                page.screenshot(path="/tmp/gemini_challenge.png")
            print(f"[+] Tentativa {tentativa + 1}: analisando...")

            try:
                resp = None
                for retry in range(3):
                    try:
                        resp = perguntar_gemini(model, "/tmp/gemini_challenge.png",
                            "You are a captcha solver. This is a 3x3 grid of images (numbered 1-9, left-to-right, top-to-bottom). "
                            "There is a reference image at the top. Look at the instruction text in Portuguese. "
                            "Identify which grid cells match the reference. "
                            "Reply with ONLY the matching cell numbers as a comma-separated list. Nothing else. "
                            "Example answer: 2,5,8")
                        break
                    except Exception as e:
                        if "429" in str(e):
                            if retry == 0:
                                print(f"[-] Quota excedida. Aguardando 10s...")
                                time.sleep(10)
                            elif retry == 1:
                                print(f"[-] Quota excedida. Aguardando 10s...")
                                time.sleep(10)
                            else:
                                print(f"[-] Quota excedida, sem retries.")
                                break
                        else:
                            raise
                if resp is None:
                    print("[-] Sem resposta do Gemini.")
                    continue
                print(f"[+] IA: {resp.strip()}")
            except Exception as e:
                print(f"[-] Erro Gemini: {e}")
                continue

            numeros = parse_captcha_answer(resp)
            if not numeros:
                print("[-] Nenhum numero na resposta.")
                continue

            print(f"[+] Numeros: {numeros}")

            challenge_frame = None
            for frame in page.frames:
                if "hcaptcha" in frame.url and "challenge" in frame.url:
                    challenge_frame = frame
                    break

            if not challenge_frame:
                print("[-] Frame do desafio nao encontrado.")
                continue

            imgs = challenge_frame.locator(".task-image")
            img_count = imgs.count()
            print(f"[+] {img_count} imagens no grid")

            for num in numeros:
                idx = num - 1
                if idx < 0 or idx >= img_count:
                    print(f"[-] Index {idx} fora do range (0-{img_count-1})")
                    continue
                try:
                    bb_img = imgs.nth(idx).bounding_box()
                    if bb_img:
                        cx = bb_img["x"] + bb_img["width"] / 2
                        cy = bb_img["y"] + bb_img["height"] / 2
                        page.mouse.move(cx, cy, steps=6)
                        time.sleep(0.1)
                        page.mouse.click(cx, cy)
                        time.sleep(0.2)
                        print(f"[+] Clicou imagem {num}")
                except Exception as e:
                    print(f"[-] Nao clicou {num}: {e}")

            time.sleep(0.3)

            try:
                submit = challenge_frame.locator("button.button-submit, [role=button].button-submit")
                if submit.count() > 0:
                    submit.first.click(timeout=3000)
                    print("[+] Clicou Verificar")
                else:
                    submit = challenge_frame.locator("div.button-submit, a.button-submit")
                    if submit.count() > 0:
                        submit.first.click(timeout=3000)
                        print("[+] Clicou Verificar")
            except Exception as e:
                print(f"[-] Erro ao clicar Verificar: {e}")

            time.sleep(2)
            page.screenshot(path=f"/tmp/gemini_after_{tentativa}.png")

            if "ConsultaPublicaExibir" in page.url:
                print("[+] Navegou para resultado!")
                resolved = True
                break

            token = page.evaluate(
                "() => { const e = document.querySelector("
                "\"textarea[name='h-captcha-response']\"); return e ? e.value : ''; }"
            )
            if token and len(token) > 100:
                print("[+] Captcha resolvido! Token obtido.")
                break

        time.sleep(1)

        if "ConsultaPublicaExibir" in page.url:
            resolved = True

        if not resolved:
            token = page.evaluate(
                "() => { const e = document.querySelector("
                "\"textarea[name='h-captcha-response']\"); return e ? e.value : ''; }"
            )
            if token:
                print(f"[+] Token encontrado: {len(token)} chars")
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

            print("[+] Consultando...")
            try:
                page.click("#id_submit", force=True)
            except Exception:
                pass
            time.sleep(2)

            if "ConsultaPublicaExibir" in page.url:
                resolved = True

        page.screenshot(path="/tmp/gemini_resultado.png", full_page=True)
        if resolved:
            print("=" * 50)
            print("SUCESSO: COMPROVANTE DE SITUACAO CADASTRAL")
            print("=" * 50)
        else:
            print("[-] Comprovante nao encontrado. Verifique /tmp/gemini_resultado.png")

        print("\n[i] Navegador aberto.")
        if sys.stdin.isatty():
            input("Enter para fechar...")


if __name__ == "__main__":
    main()
