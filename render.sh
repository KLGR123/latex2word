python3 label.py --input outputs/translated.json --output outputs/labeled.json
python3 refmap.py --input outputs/labeled.json --output outputs/refmap.json --verbose

python3 render.py --labeled outputs/labeled.json \
  --refmap outputs/refmap.json --citations outputs/citations.json --inputs-dir inputs/ \
  --output outputs/result.docx --toc --font-size 12 --page-size a4