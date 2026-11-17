@echo off
rem Installe les moteurs locaux optionnels de la chaine M1-M6 (Windows, Python 3.11+).
rem L'application fonctionne sans : chaque moteur a un repli.
setlocal
cd /d "%~dp0"

echo [1/4] Paquets Python (requirements-local.txt)
py -3 -m pip install --upgrade pip
py -3 -m pip install -r requirements-local.txt
if errorlevel 1 goto erreur

echo [2/4] Modele spaCy francais (fr_core_news_lg, ~570 Mo)
py -3 -m spacy download fr_core_news_lg
if errorlevel 1 (
  echo Echec du modele lg : tentative avec fr_core_news_md
  py -3 -m spacy download fr_core_news_md
)

echo [3/4] Modeles Hugging Face (prechargement dans le cache local)
py -3 -c "from huggingface_hub import snapshot_download as d; [d(m) for m in ('MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7','urchade/gliner_multi-v2.1','Qwen/Qwen3-Embedding-0.6B','BAAI/bge-reranker-v2-m3','sentence-transformers/paraphrase-multilingual-mpnet-base-v2','asi/gpt-fr-cased-small','segment-any-text/sat-3l-sm')]"
if errorlevel 1 echo Certains modeles n'ont pas pu etre telecharges : les replis seront utilises.

echo [4/4] Etat des moteurs
py -3 -c "import json,server as s,chaine;chaine.brancher(s);from chaine import moteurs;print(json.dumps(moteurs.etat(),indent=1,ensure_ascii=False))"

echo Termine.
goto fin

:erreur
echo Installation interrompue. L'application reste utilisable avec les replis.
exit /b 1

:fin
endlocal
