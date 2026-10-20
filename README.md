# Zeneipari állásfigyelő

Minden szerdán reggel végigjárja a hazai zeneipari cégek (fesztiválok, koncertszervezők, helyszínek, kiadók, jegyértékesítők) állásoldalait, valamint a LinkedIn, a Profession.hu és a Dreamjobs kereséseit. Kiszűri a nem zenei állásokat, és e-mailben (Brevo) elküldi az összesítőt. Az új hirdetések **ÚJ** jelölést kapnak.

## Beállítás (egyszer kell)

1. **Repó létrehozása.** Hozz létre egy új (privát) repót a GitHubon, és töltsd fel bele a mappa teljes tartalmát, a `.github` mappával együtt.
2. **Titkos adatok megadása.** *Settings → Secrets and variables → Actions → New repository secret*. Ha a Deszka-digesthez már van Brevo-kulcsod, az mehet ide is.
   | Név | Érték |
   |---|---|
   | `BREVO_API_KEY` | a Brevo API-kulcs |
   | `SENDER_EMAIL` | a Brevóban hitelesített feladó címe |
   | `RECIPIENTS` | címzett(ek), vesszővel elválasztva |
3. **Írási jog engedélyezése.** *Settings → Actions → General → Workflow permissions →* **Read and write permissions**. Ezzel jegyzi meg a program, mely hirdetéseket látta már.
4. **Első futtatás.** *Actions → Heti zeneipari állásfigyelő → Run workflow*. Az első levélben még minden talált hirdetés szerepel, az ÚJ jelölés a második héttől működik.

## Mikor fut?

- **Automatikusan:** szerdánként 7:17-kor (télen 6:17-kor).
- **Kézzel:** *Actions → Run workflow*, bármikor. Ha a „Küldjön e-mailt?” kérdésre a **nem** választ adod, csak próbafutás lesz: a levél előnézete letölthető lesz a futás oldaláról (*email-elonezet*), és semmi nem mentődik.

## Testreszabás: `sources.yaml`

Közvetlenül a GitHubon is szerkesztheted (ceruza ikon), kódolni nem kell hozzá.

- **`companies`**: a figyelt cégek. Ha egy cégnél tudod a pontos állásoldalt, add meg `careers_url`-ként.
- **`search_keywords`**: ezekre a szavakra keres a portálokon.
- **`music_terms`**: a zenei szűrő. Egy portálon talált állás csak akkor kerül a levélbe, ha a címében vagy a cégnevében szerepel valamelyik szó, vagy a cég rajta van a listán.
- **`exclude_terms`**: az ilyen szót tartalmazó állásokat mindig kihagyja (pultos, hostess, zenetanár stb.).

## A levél alja: „Figyelt cégoldalak”

| Jelzés | Jelentés |
|---|---|
| ✅ hirdetés van | az állásoldalon talált hirdetést |
| 🔔 változott | van állásoldal, a tartalma megváltozott, de nem tudta pozíciókra bontani – érdemes ránézni |
| ▫️ üres | van állásoldal, most nincs rajta semmi |
| – nincs állásoldal | a cég nem vezet ilyet, a portálokon (főleg LinkedInen) figyeljük |
| ⚠️ hiba | az oldal nem töltött be – ha tartósan így marad, érdemes javítani a címet |

## Mire nem képes

A Facebook- és Instagram-posztokat, illetve a Facebook-csoportokat nem lehet gépileg figyelni. Az Indeed rendszeresen blokkolja az automatikus lekérdezést, ezért kimaradt.
