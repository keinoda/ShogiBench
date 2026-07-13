# ShogiBench

ShogiBench is a Shogi (USI) engine testing framework based on
[OpenBench](https://github.com/AndyGrant/OpenBench), split into a cheap
always-on coordination server and on-demand high-performance workers:

- **Coordination server** — this Django app. Hosts the public UI (viewing
  tests is open to everyone), the API, the SQLite database, and the SPRT/SPSA
  bookkeeping. Designed to run on an inexpensive PaaS (Fly.io, Render) or any
  small VPS. See [`Documentation/DEPLOYMENT.md`](Documentation/DEPLOYMENT.md).
- **Workers** — rented vast.ai (or any Linux) instances that build the engines
  and play the games. They connect to the server with a dedicated **Worker
  Key** issued on the `/workers/` page, so your account password never lands
  on a rented machine. Bootstrap scripts live in [`Deploy/worker/`](Deploy/worker/).
- **Invite-only accounts** — public sign-up is disabled. Administrators create
  accounts with `python manage.py invite <username> [--approver]`. Creating
  tests, tuning, and managing networks all require a login.
- **Test creation API** — authenticated users can create the same tests as the
  `/test/new/` page through `POST /api/tests/`. See
  [`Documentation/TEST_API.md`](Documentation/TEST_API.md).

Quick start for a worker (or copy the snippet from `/workers/`):

```sh
export OPENBENCH_SERVER=https://<your-server>/
export OPENBENCH_USERNAME=<username>
export OPENBENCH_PASSWORD=<worker key token>
curl -sSL https://raw.githubusercontent.com/keinoda/ShogiBench/shogi/Deploy/worker/setup_worker.sh | bash
```

---

# OpenBench

OpenBench is an open-source Chess Engine Testing Framework for UCI engines. OpenBench provides a lightweight interface and client to facilitate running fixed-game tests as well as SPRT tests to benchmark changes to engines for performance and stability. OpenBench supports [Fischer Random Chess](https://en.wikipedia.org/wiki/Chess960).

OpenBench is the primary testing framework used for the development of [Ethereal.](https://github.com/AndyGrant/Ethereal) The primary instance of OpenBench can be found at [http://chess.grantnet.us](http://chess.grantnet.us/). The Primary instance of OpenBench supports development for
[Berserk](https://github.com/jhonnold/berserk), [Bit-Genie](https://github.com/Aryan1508/Bit-Genie), [BlackMarlin](https://github.com/dsekercioglu/blackmarlin), [Demolito](https://github.com/lucasart/Demolito), [Drofa](https://github.com/justNo4b/Drofa), [Ethereal](https://github.com/AndyGrant/Ethereal), [FabChess](https://github.com/fabianvdW/FabChess), [Halogen](https://github.com/KierenP/Halogen), [Igel](https://github.com/vshcherbyna/igel), [Koivisto](https://github.com/Luecx/Koivisto), [Laser](https://github.com/jeffreyan11/laser-chess-engine), [RubiChess](https://github.com/Matthies/RubiChess), [Seer](https://github.com/connormcmonigle/seer-nnue), [Stash](https://github.com/mhouppin/stash-bot), [Weiss](https://github.com/TerjeKir/weiss), [Winter](https://github.com/rosenthj/Winter), and [Zahak](https://github.com/amanjpro/zahak). A dozen or more engines are using their own private, local instances of OpenBench.

You can join OpenBench's [Discord server](https://discord.com/invite/9MVg7fBTpM) to join the discussion, see what developers are working on and talking about, or to find out how you can contribute to the project and become a part of it. OpenBench is heavily inspired by [Fishtest](https://github.com/glinscott/fishtest). The project is powered by the [Django Web Framework](https://www.djangoproject.com/) and [fastchess](https://github.com/Disservin/fastchess).

Documentation for OpenBench is available in the [Wiki](https://github.com/AndyGrant/OpenBench/wiki)
