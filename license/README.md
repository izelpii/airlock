# license/

Put this copy's license file here — the signed `*.license` file you were sent
(for example `2026-10-25-ana-silva-1e07aea0.license`). Keep its name or rename
it `airlock.license`; if there are several, `airlock.license` is read, otherwise
the newest.

The backend container sees this folder read-only at `/license`. A license is
read again when it changes, so there is nothing to restart: the tooltip on the
version badge beside the logo then says *Licensed to …*. With no file, or an
altered one, a red **Unlicensed - No commercial use** shows beside the logo.

It is a good-faith notice. Airlock works exactly the same with or without one.

License files are kept out of git (`.gitignore`): a license belongs to one copy,
not to the source.
