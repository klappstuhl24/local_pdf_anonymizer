# Local PDF Anonymizer

Anonymisiert PDFs mit sensiblen Informationen – vollständig lokal, ohne dass jemals Dokumentdaten den Rechner verlassen. Ein lokales Ollama-Modell ersetzt Namen, IBANs, Adressen usw. durch erfundene, formatgleiche Werte; Layout, Logos und Icons des Originals bleiben erhalten.

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

## Benutzung

Ganzen Ordner verarbeiten (Batch-Modus):

```bash
python anonymize_pdf.py vertraege -o results
```

Ergebnisstruktur:

```
results/
├── pdfs/                          # nur die fertigen anonymisierten PDFs
│   ├── Kaufvertrag1_anonymized.pdf
│   └── ...
├── output_1/                      # vollständiges Ergebnis pro Eingabe-PDF
│   ├── Kaufvertrag1_anonymized.pdf
│   ├── Kaufvertrag1_anonymized.tex   # erzeugte LaTeX-Datei
│   ├── Kaufvertrag1_mapping.json     # Ersetzungstabelle (Original → Ersatz)
│   └── assets/                       # extrahierte Bilder/Logos bzw. Scans
├── output_2/
└── ...
```

Einzelne Datei geht ebenfalls:

```bash
python anonymize_pdf.py vertrag.pdf -o results
```

Weitere Optionen: `--model <name>` (anderes Ollama-Modell), `--ollama-url <url>`, `--no-llm` (nur Layout-Rekonstruktion, zum Testen der LaTeX-Pipeline).

## Garantie: 100 % lokale Verarbeitung

Kein Byte des Dokuments verlässt den Rechner. Das lässt sich am Code und an der Architektur festmachen:

- **PyMuPDF** (Extraktion) und **Tesseract** (OCR) sind lokale Bibliotheken/Programme ohne jede Netzwerkfunktion in diesem Ablauf – sie lesen und schreiben nur Dateien auf der Festplatte.
- **Ollama** ist die einzige „Netzwerk"-Verbindung im Code: ein einziger `requests.post` an `http://localhost:11434`. Das ist die Loopback-Schnittstelle des eigenen Rechners – die Daten wandern vom Python-Prozess zum Ollama-Prozess auf derselben Maschine und berühren nie die Netzwerkkarte nach außen. Das Modell (~15 GB) liegt lokal auf der Platte und rechnet auf der lokalen Hardware.
- **tectonic** (LaTeX → PDF) sieht nur die bereits anonymisierte LaTeX-Datei. Es lädt beim allerersten Lauf einmalig generische LaTeX-Pakete herunter (Download only, kein Upload) und cached sie unter `~/Library/Caches/Tectonic`; danach läuft die Kompilierung komplett offline.

Nach dem einmaligen Setup funktioniert die gesamte Pipeline nachweislich ohne Internetverbindung – einfach WLAN ausschalten und laufen lassen.

## Workflow

1. **Extraktion** – Jede PDF-Seite wird mit PyMuPDF zerlegt: Textabschnitte mit exakter Position, Schriftgröße, Schriftschnitt und Farbe; eingebettete Bilder/Logos/Icons als PNG; Vektorgrafiken (Linien, Rahmen, Tabellen). Gescannte Seiten (kein extrahierbarer Text) werden automatisch erkannt und per Tesseract-OCR mit Wort-Koordinaten gelesen.
2. **Anonymisierung** – Der Dokumenttext geht an das lokale Ollama-Modell, das eine Ersetzungstabelle liefert: jede sensible Angabe → erfundener, formatgleicher Wert (gleiches Format, ähnliche Länge, konsistent über das ganze Dokument). Die Tabelle wird als JSON gespeichert.
3. **LaTeX-Rekonstruktion** – Jedes Element wird per TikZ-Overlay an seiner Originalposition platziert (Einheit `bp` = PDF-Punkte, Ursprung oben links), dadurch bleibt das Layout pixelgenau erhalten. Bei Scans bleibt das Seitenbild als Hintergrund; sensible Stellen werden in der gesampelten Papierfarbe übermalt und mit dem Ersatztext an der Original-Grundlinie überschrieben.
4. **Kompilierung** – Die LaTeX-Datei wird mit tectonic (alternativ latexmk/pdflatex) zur fertigen PDF kompiliert und zusätzlich nach `results/pdfs/` kopiert.

## Hinweise

- Das Modell kann Angaben übersehen – `*_mapping.json` und die fertige PDF vor Weitergabe immer prüfen.
- Bei Scans hängt die Erkennung an der OCR-Qualität; Handschriftliches (z. B. Unterschriften) wird nicht erfasst und bleibt im Bild stehen.
- Die Original-Schriftart wird bei digitalen PDFs nur klassifiziert (Serifen/serifenlos/Monospace, fett/kursiv), nicht 1:1 übernommen.
