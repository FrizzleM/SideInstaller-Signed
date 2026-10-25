# Custom certificates

Drop your own signing certificates in here and the **sign**
workflow picks them up alongside the public pools listed in
[`cert-url.txt`](../cert-url.txt). Committing anything to this folder starts a
run on its own, which signs the new certificate (and only that) for the live
releases and adds its card to the page, usually within a couple of minutes.

This folder is listed **first** in `cert-url.txt`, which matters: when a
certificate here has the same name as one in a public pool, the first source
wins. So name your folder like the public one (e.g. `HSBC Bank plc`) and your
copy is the one that gets signed.

## Folder structure

One folder per certificate, named however you want the certificate to appear on
the install page:

```
certs/
├── README.md                       ← this file, ignored
├── My Cert/
│   ├── My Cert.p12                 ← required
│   ├── My Cert.mobileprovision     ← required
│   └── password.txt                ← optional (see below)
└── Another Cert/
    ├── Another Cert.p12
    ├── Another Cert.mobileprovision
    └── password.txt
```

Rules:

- **The folder name is the output name.** `My Cert/` produces
  a `sideinstaller-My-Cert.ipa` asset on the release and a card labelled `My Cert`. Spaces and
  other awkward characters are replaced with `-` in the filename only.
- **The `.p12` and `.mobileprovision` should share the folder's name.** If they
  don't, the folder name still wins for the output and a warning is logged. If
  the profile has a different name entirely, the only `.mobileprovision` in the
  folder is used regardless.
- **Both files are required.** A folder with a `.p12` but no `.mobileprovision`
  is not signed; it is listed under "Not signed" in the release notes, as is a
  wrong password or a profile that was issued for a different certificate.
- **Extra files are ignored.** Notes, `.txt` files, `NexStore/` subfolders and
  anything else can sit alongside without affecting anything.

A `.p12` dropped loose at the top of `certs/` (no folder) also works — it is
named after the file instead of the folder — but it needs its
`.mobileprovision` next to it, and the per-folder layout is the tidier option.

## Passwords

If the `.p12` has a password, put it in a `password.txt` inside that
certificate's folder — the whole file is the password, or a `password: hunter2`
line if you prefer to keep notes around it. These filenames are checked, in
order:

```
<Name>.password    <Name>.pass    <Name>.txt
password.txt       password
readme.txt         README.txt     readme
```

With none of them present the fallback is the `P12_PASSWORD` env var, and
failing that the public pools' default (`WSF`) — which is almost certainly not
your password, so add a `password.txt` for anything of your own.

## Changing or removing a certificate

Replace its files and commit: the run that follows re-signs that certificate
alone. Delete its folder and commit: the run removes its IPA from the live
releases and its card from the page.

## Before you commit

A `.p12` is a **private key**. If this repository is public, anything you put
here is public too, password file included, and anyone can sign apps as you.
Only upload certificates you are willing to hand out — for anything else, keep
the pool source private and point `cert-url.txt` at it instead, or pass it in
for a single run with the workflow's `cert_zip_url` input.
