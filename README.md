# Local PDF Anonymizer

Anonymisiert PDFs mit sensiblen Informationen – vollständig lokal, ohne dass jemals Dokumentdaten den Rechner verlassen. Ein lokales Ollama-Modell ersetzt Namen, IBANs, Adressen usw. durch erfundene, formatgleiche Werte; Unterschriften werden per YOLO erkannt und entfernt. Layout, Logos und Icons des Originals bleiben erhalten.

## Installation

Voraussetzungen (macOS, via [Homebrew](https://brew.sh)):

```bash
# 1. Ollama + Modell (läuft komplett lokal)
brew install ollama
ollama pull mistral-small3.2:24b

# 2. LaTeX-Compiler
brew install tectonic

# 3. OCR für gescannte PDFs (inkl. deutschem Sprachpaket)
brew install tesseract tesseract-lang

# 4. Python-Umgebung
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Beim **ersten Lauf** lädt das Skript automatisch ein YOLO-Unterschriftsmodell von Hugging Face (Fallback: [Mels22/Signature-Detection-Verification](https://huggingface.co/Mels22/Signature-Detection-Verification)). Optional kann das YOLOv8-Modell [tech4humans/yolov8s-signature-detector](https://huggingface.co/tech4humans/yolov8s-signature-detector) nach Lizenzannahme und `huggingface-cli login` unter `models/yolov8s.pt` abgelegt werden.

## Benutzung

Ganzen Ordner verarbeiten (Batch-Modus):

```bash
python anonymize_pdf.py vertraege -o results
```

Einzelne Datei:

```bash
python anonymize_pdf.py vertrag.pdf -o results
```

Ergebnisstruktur:

```
results/
├── pdfs/                              # nur die fertigen anonymisierten PDFs
│   ├── Kaufvertrag1_anonymized.pdf
│   └── ...
├── output_1/                          # vollständiges Ergebnis pro Eingabe-PDF
│   ├── Kaufvertrag1_anonymized.pdf
│   ├── Kaufvertrag1_anonymized.tex    # erzeugte LaTeX-Datei
│   ├── Kaufvertrag1_mapping.json      # Ersetzungstabelle (Original → Ersatz)
│   ├── Kaufvertrag1_run.log.txt        # Lauf-Log (Konsole + Datei)
│   └── assets/                        # extrahierte Bilder, Vektor-PNGs, Scans
│       ├── p0_img42.png               # eingebettete Bilder
│       ├── p0_vec0.png                # gerasterte Vektor-Cluster
│       └── p1_scan.png                # Scan-Hintergründe
├── output_2/
└── ...
```

### CLI-Optionen

| Option | Beschreibung |
|---|---|
| `-o`, `--outdir` | Ausgabeordner (Standard: `results/`) |
| `--model` | Ollama-Modell (Standard: `mistral-small3.2:24b`) |
| `--ollama-url` | Ollama-Server (Standard: `http://localhost:11434`) |
| `--no-llm` | Nur Layout-Rekonstruktion, keine Text-Anonymisierung |
| `--no-signature-filter` | Unterschrifts-Erkennung deaktivieren |
| `--signature-conf` | YOLO-Konfidenzschwelle, 0–1 (Standard: `0.22`; niedriger = mehr Treffer) |
| `--signature-model` | Pfad zu lokaler YOLO-`.pt`-Datei |

### Konfiguration im Code

In `anonymize_pdf.py`:

```python
INFERENCE_DEVICE = "gpu"              # "gpu" oder "cpu"
SIGNATURE_CONF_DEFAULT = 0.22         # niedriger = mehr Unterschriften erkannt vs. mehr false Positives
```

Per CLI noch feiner einstellen:

```bash
python anonymize_pdf.py vertrag.pdf -o results --signature-conf 0.15
```

Der Ollama-System-Prompt liegt in **`prompts/ollama_system_prompt.md`** und kann dort ohne Code-Änderung angepasst werden.

## Workflow

1. **Extraktion** – PyMuPDF zerlegt jede Seite: Text mit Position, Schrift und Farbe; eingebettete Bilder als PNG; Vektorgrafiken (Linien, Kurven, Rahmen). Nahe Vektorpfade werden zu Clustern zusammengefasst und als PNG in `assets/` gerastert. Gescannte Seiten werden per Tesseract-OCR mit Wort-Koordinaten gelesen.
2. **Unterschriften filtern (YOLO)** – Alle extrahierten Bilder und Vektor-PNGs werden auf handschriftliche Unterschriften geprüft. Treffer werden aus dem LaTeX/PDF entfernt; auf Scan-Seiten werden erkannte Unterschrifts-Regionen mit Papierfarbe übermalt.
3. **Anonymisierung (Ollama)** – Der Dokumenttext geht an das lokale LLM, das eine Ersetzungstabelle liefert (sensible Angabe → erfundener, formatgleicher Wert). Die Tabelle wird als JSON gespeichert.
4. **LaTeX-Rekonstruktion** – Jedes Element wird per TikZ-Overlay an der Originalposition platziert. Bei Scans bleibt das Seitenbild als Hintergrund; sensible Textstellen werden übermalt und mit Ersatztext an der Original-Grundlinie überschrieben.
5. **Kompilierung** – Die LaTeX-Datei wird mit tectonic (alternativ latexmk/pdflatex) zur PDF kompiliert und nach `results/pdfs/` kopiert.

## Garantie: 100 % lokale Verarbeitung

Kein Byte des Dokuments verlässt den Rechner im normalen Betrieb:

- **PyMuPDF**, **Tesseract** und **YOLO (Ultralytics/PyTorch)** rechnen lokal auf der GPU (Apple MPS / CUDA) oder CPU.
- **Ollama** ist die einzige Netzwerkverbindung im Code: `requests.post` an `http://localhost:11434` (Loopback). Das Modell liegt lokal und rechnet auf der lokalen Hardware.
- **tectonic** kompiliert nur die anonymisierte LaTeX-Datei. Beim ersten Lauf werden LaTeX-Pakete einmalig heruntergeladen und gecacht; danach offline möglich.

**Einmaliger Download:** YOLO-Gewichte von Hugging Face beim ersten Start (danach im Cache). Optional vorab offline nutzbar durch Ablegen unter `models/yolov8s.pt`.

Nach dem Setup funktioniert die Pipeline ohne Internet – WLAN aus, `ollama serve` läuft, fertig.

## Hinweise

- Das LLM kann Angaben übersehen – `*_mapping.json` und die fertige PDF vor Weitergabe immer prüfen.
- Unterschriftserkennung ist heuristisch (YOLO); Stempel oder Kritzeleien können False Positives erzeugen – ggf. `--signature-conf` anpassen.
- Vektor-Unterschriften werden über gerasterte Cluster erkannt; reine Tabellenlinien und Seitenfüllungen werden weitgehend ausgefiltert.
- Bei Scans hängt die Texterkennung an der OCR-Qualität.
- Die Original-Schriftart wird bei digitalen PDFs nur klassifiziert (Serifen/serifenlos/Monospace, fett/kursiv), nicht 1:1 übernommen.
