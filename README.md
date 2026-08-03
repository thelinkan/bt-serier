# STUPA-serier

En första desktopversion för att:

- läsa matchprogram från publika STUPA-sidor,
- spara matcher lokalt i SQLite,
- filtrera på arrangör,
- sammanställa per serie, omgång och datum,
- exportera det filtrerade resultatet till CSV.

## Installation på Windows

Öppna Kommandotolken eller PowerShell i projektmappen:

```powershell
py -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Starta sedan programmet:

```powershell
python -m stupa_serier
```

## Första testet

1. Öppna en serie i STUPA.
2. Kopiera adressen från webbläsaren.
3. Klistra in adressen i programmet.
4. Ange ett serienamn, exempelvis `Division 1 herrar östra`.
5. Klicka på **Hämta serie**.
6. Sök efter exempelvis `Stratos` i arrangörsfiltret.

## Diagnostik

Vid varje hämtning sparas följande under mappen `diagnostics`:

- en skärmbild av sidan,
- sidans renderade HTML,
- all synlig text,
- de råa matchrader som skraparen hittade.

Om STUPA ändrar struktur eller en matchrad inte tolkas rätt kan dessa filer användas
för att anpassa selektorerna utan att behöva gissa.

## Begränsning i första versionen

Programmet hämtar en angiven serieadress åt gången. Nästa steg är automatisk upptäckt
av samtliga nationella serier och distriktsserier från startsidan. Det steget bör göras
när vi har verifierat navigeringen och HTML-strukturen med diagnostik från en riktig körning.
