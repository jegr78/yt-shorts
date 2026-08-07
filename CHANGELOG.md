# Changelog

## [0.1.1](https://github.com/jegr78/yt-shorts/compare/v0.1.0...v0.1.1) (2026-08-07)


### Bug Fixes

* **hooks:** drop heredoc bodies before deciding a merge happened ([#22](https://github.com/jegr78/yt-shorts/issues/22)) ([051014e](https://github.com/jegr78/yt-shorts/commit/051014e06a8eebb8edd1fc00fe1993721f131076))

## 0.1.0 (2026-08-07)


### Features

* **windows:** make owner-only file permissions real on Windows ([c9bca7a](https://github.com/jegr78/yt-shorts/commit/c9bca7a747a47ca5669d5c1082def5ab7ae3a5b8))


### Bug Fixes

* **ci:** exclude the committed JS bundle from CodeQL and pin its exact version ([6b9b13d](https://github.com/jegr78/yt-shorts/commit/6b9b13d3e42b90779211f4535e5d904c10e850c5))
* **ci:** the ruleset needs twelve required checks, not eight ([b25b962](https://github.com/jegr78/yt-shorts/commit/b25b962c0ac23d29dc601ac3b2c2ed72ef835e51))
* clear the CodeQL findings without switching queries off ([1f15903](https://github.com/jegr78/yt-shorts/commit/1f1590327806a3326532ce6ca1204940c7f76f34))
* cut the subtitle track to its timeline instead of trusting the last entry ([#13](https://github.com/jegr78/yt-shorts/issues/13)) ([fb0dff5](https://github.com/jegr78/yt-shorts/commit/fb0dff5bd3155bb4990f78d5e0db8907e656beb4))
* **packaging:** add missing pytest-playwright to dev extras ([e81c911](https://github.com/jegr78/yt-shorts/commit/e81c9112f7c177815386d7351258be27003f0fab))
* remove every absolute local path, and guard against their return ([d2f35b2](https://github.com/jegr78/yt-shorts/commit/d2f35b28f90c7a78833c9528ccc0389de35f7834))
* **tests:** the matrix and E2E-guard checks could not fail on the mutations they exist to catch ([fc626b8](https://github.com/jegr78/yt-shorts/commit/fc626b89a99a711e8c657392fb28303bc46932bd))
* two defects the first CI run found, on platforms the local suite never sees ([03b1ff5](https://github.com/jegr78/yt-shorts/commit/03b1ff5194964d3dcaccc85778e5090f8cb59ef5))
* **wiki:** check the links pointing INTO the wiki, and the repo URL shapes that were skipped ([#16](https://github.com/jegr78/yt-shorts/issues/16)) ([2e15c4d](https://github.com/jegr78/yt-shorts/commit/2e15c4daf01f681771f9ca01325464c4eddc4f54))
* **windows:** compare SIDs, not localised account names ([bd86d43](https://github.com/jegr78/yt-shorts/commit/bd86d432eb7287708b7de506736eab588cc087f0))
* **windows:** explicit ACL reset, tasklist bytes, and the shebang shim ([1af1b14](https://github.com/jegr78/yt-shorts/commit/1af1b1477325f7004d28513db0d8ef9b1f21acb3))
* **windows:** icacls (OI)(CI) is directory-only, and it locked files out ([bdc1719](https://github.com/jegr78/yt-shorts/commit/bdc171977c30e4431d558abd032dccc306962a97))
* **windows:** pid liveness and logo paths; stop CI running the E2E suite twice ([69ffdc4](https://github.com/jegr78/yt-shorts/commit/69ffdc4ba61faaea49738f26bf5678abe1f66f3b))
* **windows:** the last one - rolloverAt = 1 names an archive from a pre-1970 time ([84eec3c](https://github.com/jegr78/yt-shorts/commit/84eec3c5376404f8ae081b047969c2cf1a3b467b))
* **windows:** the remaining failures were POSIX assumptions in the tests ([bb514c9](https://github.com/jegr78/yt-shorts/commit/bb514c940e2dc17ed67c0b856f1297fdf5337c41))


### Miscellaneous Chores

* start the release history at 0.1.0, not 0.1.1 ([a2722d7](https://github.com/jegr78/yt-shorts/commit/a2722d75a72d153f85263fe21c61a9a5f37b69d4))
