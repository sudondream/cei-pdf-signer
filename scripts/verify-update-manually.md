# Verificarea manuala a actualizarii automate

Testele automate acopera fiecare bucata, dar nu si intregul: o aplicatie
semnata real care se inlocuieste in /Applications si se redeschide. Se face o
data inainte de fiecare release care atinge `updater.py`, `prefs.py` sau
scriptul de relansare.

Dureaza ~5 minute.

## 1. Pregatire

Se construieste release-ul curent, semnat si notarizat:

    SIGN_IDENTITY="Developer ID Application: ..." \
    NOTARY_PROFILE="..." ./build-release.sh

Se instaleaza versiunea ANTERIOARA in /Applications (dezarhivata din release-ul
precedent de pe GitHub), nu cea proaspata. Fara asta nu exista nimic de
actualizat.

## 2. Verificarea ofertei

- Se deschide aplicatia din /Applications.
- Dupa ~3 secunde apare bannerul "Versiunea vX.Y-beta este disponibila".
- Se apasa x. Bannerul dispare, in header apare pastila "↑ Actualizeaza".
  Oferta NU trebuie sa dispara complet.

## 3. Verificarea actualizarii

- Se apasa pastila.
- Bara de progres avanseaza, apoi "Se verifica semnatura...", apoi
  "Repornim aplicatia...".
- Fereastra se inchide si se redeschide singura in ~15 secunde.
- Versiunea din Info.plist este cea noua:

      /usr/libexec/PlistBuddy -c 'Print :CEIReleaseTag' \
          '/Applications/CEI PDF Signer.app/Contents/Info.plist'

- NU trebuie sa ramana nimic in urma:

      ls -d '/Applications/CEI PDF Signer.app.old-'* 2>/dev/null
      ls -d "${TMPDIR}cei-update-"* 2>/dev/null

  Ambele trebuie sa nu gaseasca nimic.

- Aplicatia reinstalata este in continuare acceptata de Gatekeeper:

      spctl -a -t exec -vv '/Applications/CEI PDF Signer.app'

  Trebuie sa spuna `accepted` si `source=Notarized Developer ID`.

## 4. Verificarea mutarii in Applications

- Se sterge /Applications/CEI PDF Signer.app.
- Se dezarhiveaza release-ul in ~/Downloads si se deschide de acolo.
- Apare dialogul nativ "Muta in Applications".
- Se apasa Cancel: aplicatia porneste normal si nu mai intreaba niciodata,
  nici dupa repornire. Se verifica:

      cat ~/Library/Application\ Support/ro.cei.pdfsigner/prefs.json

- Se sterge acel fisier, se redeschide din ~/Downloads, se apasa OK:
  aplicatia se inchide si se redeschide din /Applications.

Daca aplicatia a fost deschisa direct din arhiva (translocare), copia din
~/Downloads ramane pe loc intentionat - nu este o scapare. Vezi comentariul din
`offer_move_to_applications`.

## 5. Verificarea degradarii

- Se dezarhiveaza release-ul intr-un folder fara drept de scriere si se deschide
  de acolo. Bannerul trebuie sa spuna "Descarca", nu "Actualizeaza", iar
  apasarea lui deschide pagina de release in browser.
