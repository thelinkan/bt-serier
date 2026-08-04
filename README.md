# STUPA-serier

STUPA-serier är ett desktopprogram för att hämta, lagra och analysera matchprogram från publika seriesidor i STUPA.

Programmet är skrivet i Python med PySide6. Matcherna sparas lokalt i en SQLite-databas och kan filtreras, sammanställas och exporteras utan att STUPA behöver öppnas för varje sökning.

## Funktioner

Programmet kan:

- registrera nationella och regionala källsidor,
- upptäcka serier som finns på en källsida,
- uppdatera en markerad källsida eller alla källsidor samtidigt,
- hämta matcher, omgångar, lag, arrangörer och resultat,
- spara uppgifter om säsong, serietyp och region,
- visa datum i formatet `YYYY-MM-DD`,
- filtrera matcher på arrangör, serie, säsong och månad,
- sammanställa matcher per seriehelg och omgång,
- exportera filtrerade matcher till CSV,
- visa föreningar, lag och serietillhörighet för en vald säsong,
- visa alla matcher för en förenings lag,
- filtrera en förenings matcher på månad eller lag,
- markera matcher som den valda föreningen arrangerar,
- spara diagnostikfiler vid hämtning.

Alla tabeller är skrivskyddade. Källsidor redigeras via formuläret på fliken **Källsidor**.

## Installation

### Krav

- Python 3.11 eller senare
- Windows eller Linux
- internetanslutning vid uppdatering från STUPA

### Installera på Windows

Öppna PowerShell eller Kommandotolken i projektmappen och kör:

```powershell
py -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Starta programmet med:

```powershell
python -m stupa_serier
```

### Installera på Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Starta programmet med:

```bash
python -m stupa_serier
```

## Programmets flikar

### Källsidor

Här registreras de STUPA-sidor som programmet ska hämta serier från.

Det går att:

- skapa en ny källsida,
- redigera en befintlig källsida,
- ta bort en källsida,
- uppdatera markerad källsida,
- uppdatera alla källsidor,
- öppna mappen med diagnostikfiler.

### Matcher och arrangörer

Fliken innehåller två vyer:

- **Per seriehelg/omgång** – sammanställning med antal matcher per datum, serie, omgång och arrangör.
- **Alla matcher** – varje enskild match.

Följande filter finns:

- arrangör,
- serie,
- säsong,
- månad.

Månaderna visas i säsongsordning från juli till juni och endast månader som finns i databasen visas.

Det filtrerade matchresultatet kan exporteras till CSV.

### Förening och lag

Den här fliken är alltid filtrerad på en bestämd säsong.

Till vänster visas en alfabetiskt sorterad lista över föreningar. Föreningsregistret byggs av:

- lagens namn,
- eventuella föreningsuppgifter från STUPA,
- arrangörsnamn.

Vanliga namnvarianter och klubbförkortningar normaliseras för att minska antalet dubletter.

När en förening väljs visas två underflikar till höger:

#### Lag

Visar:

- lagets namn,
- vilken serie laget spelar i,
- om serien är nationell eller regional,
- region för regionala serier.

#### Matcher

Visar alla matcher för den valda föreningens lag under vald säsong.

Matchlistan kan filtreras på:

- månad, eller
- ett bestämt lag.

Checkboxen **Markera matcher som föreningen arrangerar** markerar de rader där arrangören motsvarar den valda föreningen.

## Lägga till en källsida

En källsida representerar en STUPA-sida med en uppsättning serier. Normalt behövs en källsida för de nationella serierna och en källsida för varje region som ska följas.

### 1. Öppna en fungerande serie i STUPA

Öppna en konkret serie i webbläsaren och kopiera hela adressen från adressfältet.

Exempel på en fullständig adress:

```text
https://sbtfeventsott.stupaevents.com/events/435/1186/2/7/7
```

Använd en adress där en riktig serie visas. Enbart evenemangets rotadress, exempelvis `/events/435`, är normalt inte tillräcklig som startadress.

### 2. Gå till fliken Källsidor

Klicka på **Ny** för att tömma formuläret.

### 3. Fyll i uppgifterna

#### Namn

Ett beskrivande namn för källsidan.

Exempel:

```text
Nationella serier
```

eller:

```text
Nordöstra Svealand
```

#### Startadress

Klistra in den fullständiga STUPA-adressen till en fungerande serie på sidan.

#### Typ

Välj:

- **Nationella serier**, eller
- **Regionala serier**.

Typen styr hur programmet tolkar serieväljarna på STUPA-sidan.

#### Säsong

Ange säsongen i samma format för alla källsidor som hör till samma säsong.

Rekommenderat format:

```text
2026/2027
```

Säsongen används vid filtrering och för att hålla lag och föreningar åtskilda mellan olika spelår.

#### Region

Fylls endast i för regionala källsidor.

Exempel:

```text
Nordöstra Svealand
```

För nationella källsidor lämnas regionen tom.

### 4. Spara källsidan

Klicka på **Spara**.

Källsidan visas nu i tabellen och kan användas vid uppdatering.

## Uppdatera källsidor

### Uppdatera en källsida

1. Markera källsidan i tabellen.
2. Klicka på **Uppdatera markerad källsida**.

Programmet upptäcker först vilka serier som finns på sidan och hämtar sedan serierna en i taget.

### Uppdatera alla källsidor

Klicka på **Uppdatera alla källsidor**.

Det passar när hela databasen ska uppdateras inför en ny seriehelg eller när flera regioner följs.

## Exempel på källsidor

### Nationell källsida

```text
Namn: Nationella serier
Typ: Nationella serier
Säsong: 2026/2027
Region: [tomt]
Startadress: [fullständig adress till en nationell serie]
```

### Regional källsida

```text
Namn: Nordöstra Svealand
Typ: Regionala serier
Säsong: 2026/2027
Region: Nordöstra Svealand
Startadress: https://sbtfeventsott.stupaevents.com/events/435/1186/2/7/7
```

## Lokal data

Programmet skapar följande mappar i arbetskatalogen:

```text
data/
diagnostics/
```

SQLite-databasen ligger normalt här:

```text
data/stupa_serier.sqlite
```

Databasen innehåller importerade matcher och registrerade källsidor.

Ta en säkerhetskopia av databasen innan större ändringar eller tester.

## Diagnostik

Vid hämtning sparar programmet diagnostik i mappen `diagnostics`.

Filerna kan bland annat innehålla:

- skärmbilder,
- renderad HTML,
- synlig text,
- råa matchrader,
- upptäckta seriealternativ,
- identifierade kopplingar mellan lag och föreningar.

Diagnostiken är viktig när STUPA ändrar sidstruktur eller när en serie, förening eller match inte tolkas korrekt.

## Viktigt om föreningsnamn

STUPA använder ibland olika namnformer för samma förening, exempelvis kortnamn, fullständiga namn och förkortningar. Programmet försöker slå ihop entydiga varianter, men föreningsregistret bygger fortfarande på den information som finns i de importerade sidorna.

Kontrollera därför fliken **Förening och lag** efter import, särskilt när nya regioner eller säsonger läggs till.

## Begränsningar

- Programmet är beroende av STUPA:s publika webbgränssnitt och kan behöva anpassas om sidstrukturen ändras.
- Det finns inget officiellt API som används av programmet.
- Föreningsmatchning bygger på namn och normaliseringsregler och kan i ovanliga fall behöva justeras.
- En källsida måste utgå från en fungerande, fullständig serieadress.

Det kan ta ganska lång stund att göra importen från en källsida.
