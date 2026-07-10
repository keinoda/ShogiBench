# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                                                             #
#   OpenBench is a chess engine testing framework authored by Andrew Grant.   #
#   <https://github.com/AndyGrant/OpenBench>           <andrew@grantnet.us>   #
#                                                                             #
#   OpenBench is free software: you can redistribute it and/or modify         #
#   it under the terms of the GNU General Public License as published by      #
#   the Free Software Foundation, either version 3 of the License, or         #
#   (at your option) any later version.                                       #
#                                                                             #
#   OpenBench is distributed in the hope that it will be useful,              #
#   but WITHOUT ANY WARRANTY; without even the implied warranty of            #
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the             #
#   GNU General Public License for more details.                              #
#                                                                             #
#   You should have received a copy of the GNU General Public License         #
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.     #
#                                                                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

from django.db.models import CharField, IntegerField, BigIntegerField, BooleanField, FloatField
from django.db.models import JSONField, ForeignKey, DateTimeField, OneToOneField, TextField
from django.db.models import CASCADE, PROTECT, Model, TextChoices
from django.contrib.auth.models import User

import hashlib

class Engine(Model):

    name     = CharField(max_length=128)
    source   = CharField(max_length=1024)
    sha      = CharField(max_length=64)
    bench    = IntegerField(default=0)

    def __str__(self):
        return '{0} ({1})'.format(self.name, self.bench)

class Profile(Model):

    user     = ForeignKey(User, PROTECT, related_name='user')
    games    = IntegerField(default=0)
    tests    = IntegerField(default=0)
    repos    = JSONField(default=dict, blank=True, null=True)
    engine   = CharField(max_length=128, blank=True)
    enabled  = BooleanField(default=False)
    approver = BooleanField(default=False)
    updated  = DateTimeField(auto_now=True)

    def __str__(self):
        return self.user.__str__()

class WorkerKey(Model):

    # Dedicated credential for connecting worker machines (e.g. vast.ai
    # instances) to the server. The worker passes the owner's username and
    # this token in place of the account password, so the real password
    # never has to be copied onto a rented machine. Tokens only authorize
    # the client endpoints; they can never log into the website.

    user      = ForeignKey(User, PROTECT, related_name='worker_keys')
    name      = CharField(max_length=64)
    token     = CharField(max_length=64, unique=True)
    enabled   = BooleanField(default=True)
    created   = DateTimeField(auto_now_add=True)
    last_used = DateTimeField(blank=True, null=True)

    def __str__(self):
        return '%s (%s)' % (self.name, self.user.username)

class BuildVariant(Model):

    # User-defined build variants, merged with the static ones defined in
    # the engine's json config. Created on the /builds/ page by pasting a
    # build command, which gets normalized into plain make arguments.

    engine  = CharField(max_length=64)
    name    = CharField(max_length=64)
    args    = CharField(max_length=512)
    author  = CharField(max_length=64)
    created = DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('engine', 'name')

    def __str__(self):
        return '[%s] %s: %s' % (self.engine, self.name, self.args)

class TuneKit(Model):

    # .tune キット: YaneuraOu 系ソースに TUNE マクロを注入して探索パラメータを
    # USI option 化するためのパッチ定義 (.tune) と、その .params。
    # SPSA (rshogi ラッパー) 作成時にキットを選ぶと、ワーカーがビルド前に
    # ソースへパッチを当てて TUNE ビルドを作る。master が進んで context が
    # ずれたときは /tunekits/ ページの照合・自動追随で更新する

    engine      = CharField(max_length=64)
    name        = CharField(max_length=64)
    author      = CharField(max_length=64)
    tune_text   = TextField()
    params_text = TextField(blank=True, default='')
    created     = DateTimeField(auto_now_add=True)
    updated     = DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('engine', 'name')

    def __str__(self):
        return '[%s] %s' % (self.engine, self.name)

    def content_sha(self):
        # TUNE ビルドのバイナリキャッシュを区別するためのハッシュ
        data = (self.tune_text + '\0' + self.params_text).encode('utf-8')
        return hashlib.sha256(data).hexdigest()[:8].upper()

class SSHCredential(Model):

    # Server-side SSH keypair used by the "Connect over SSH" feature on the
    # /workers/ page. The user registers the public key with their machine
    # provider (e.g. vast.ai account SSH keys), after which the server can
    # bootstrap a worker on an instance from just its host:port.

    user        = OneToOneField(User, CASCADE, related_name='ssh_credential')
    private_key = CharField(max_length=8192)
    public_key  = CharField(max_length=1024)
    created     = DateTimeField(auto_now_add=True)

    def __str__(self):
        return 'SSH key for %s' % (self.user.username)

class Machine(Model):

    user      = ForeignKey(User, PROTECT, related_name='owner')
    mnps      = FloatField(default=0.00)
    dev_mnps  = FloatField(default=0.00)
    base_mnps = FloatField(default=0.00)
    updated   = DateTimeField(auto_now=True)
    secret    = CharField(max_length=64, default='None')
    info      = JSONField()
    workload  = IntegerField(default=0)

    def __str__(self):
        return '[%d] %s' % (self.id, self.user.username)

class Result(Model):

    test     = ForeignKey('Test', PROTECT, related_name='test')
    machine  = ForeignKey('Machine', PROTECT, related_name='machine')
    updated  = DateTimeField(auto_now=True)

    # Trinomial Distributions
    losses = IntegerField(default=0)
    draws  = IntegerField(default=0)
    wins   = IntegerField(default=0)

    # Pentanomial Distributions
    LL = IntegerField(default=0)
    LD = IntegerField(default=0)
    DD = IntegerField(default=0)
    DW = IntegerField(default=0)
    WW = IntegerField(default=0)

    # Overall collection of Results
    games    = IntegerField(default=0)
    crashes  = IntegerField(default=0)
    timeloss = IntegerField(default=0)

    def __str__(self):
        return '{0} {1}'.format(self.test.dev.name, self.machine.__str__())

class Test(Model):

    class ScaleMethod(TextChoices):
        DEV  = 'DEV' , 'DEV'
        BASE = 'BASE', 'BASE'
        BOTH = 'BOTH', 'BOTH'

    # Misc information
    author      = CharField(max_length=64)
    upload_pgns = CharField(max_length=16, default='FALSE')

    # Opening book settings
    book_name  = CharField(max_length=32)
    book_index = IntegerField(default=1)

    # Dev Engine, and all of its settings
    dev              = ForeignKey('Engine', PROTECT, related_name='dev')
    dev_repo         = CharField(max_length=1024)
    dev_engine       = CharField(max_length=64)
    dev_options      = CharField(max_length=256)
    dev_network      = CharField(max_length=256, blank=True)
    dev_netname      = CharField(max_length=256, blank=True)
    dev_time_control = CharField(max_length=32)
    dev_build_name   = CharField(max_length=64,  default='default')
    dev_build_args   = CharField(max_length=512, blank=True, default='')

    # Optional label shown instead of the auto-derived branch/net name,
    # for tests whose sides would otherwise be indistinguishable
    dev_display      = CharField(max_length=64, blank=True, default='')

    # Base Engine, and all of its settings
    base              = ForeignKey('Engine', PROTECT, related_name='base')
    base_repo         = CharField(max_length=1024)
    base_engine       = CharField(max_length=64)
    base_options      = CharField(max_length=256)
    base_network      = CharField(max_length=256, blank=True)
    base_netname      = CharField(max_length=256, blank=True)
    base_time_control = CharField(max_length=32)
    base_build_name   = CharField(max_length=64,  default='default')
    base_build_args   = CharField(max_length=512, blank=True, default='')
    base_display      = CharField(max_length=64, blank=True, default='')

    # Changable Test Parameters
    workload_size = IntegerField(default=32)
    priority      = IntegerField(default=0)
    throughput    = IntegerField(default=0)

    # Scaling Mechanisms
    scale_method  = CharField(max_length=16, choices=ScaleMethod.choices, default=ScaleMethod.BASE)
    scale_nps     = IntegerField(default=0)

    # Tablebases and Match runner adjudicatoins
    syzygy_wdl  = CharField(max_length=16, default='OPTIONAL')
    syzygy_adj  = CharField(max_length=16, default='OPTIONAL')
    win_adj     = CharField(max_length=64, default='movecount=3 score=400')
    draw_adj    = CharField(max_length=64, default='movenumber=40 movecount=8 score=10')

    # Test Mode specific values, either SPRT, GAMES, SPSA, or DATAGEN
    test_mode     = CharField(max_length=16, default='SPRT')
    elolower      = FloatField(default=0.0) # SPRT
    eloupper      = FloatField(default=0.0) # SPRT
    alpha         = FloatField(default=0.0) # SPRT
    beta          = FloatField(default=0.0) # SPRT
    lowerllr      = FloatField(default=0.0) # SPRT
    currentllr    = FloatField(default=0.0) # SPRT
    upperllr      = FloatField(default=0.0) # SPRT
    max_games     = IntegerField(default=0) # GAMES or DATAGEN
    spsa          = JSONField(default=dict, blank=True, null=True) # SPSA
    genfens_args  = CharField(max_length=256, default='', blank=True) # DATAGEN
    play_reverses = BooleanField(default=False) # DATAGEN

    # Collection of all individual Result() objects
    games  = IntegerField(default=0) # Overall
    losses = IntegerField(default=0) # Trinomial
    draws  = IntegerField(default=0) # Trinomial
    wins   = IntegerField(default=0) # Trinomial
    LL     = IntegerField(default=0) # Pentanomial
    LD     = IntegerField(default=0) # Pentanomial
    DD     = IntegerField(default=0) # Pentanomial
    DW     = IntegerField(default=0) # Pentanomial
    WW     = IntegerField(default=0) # Pentanomial

    # Switching all future tests to Pentanomial
    use_tri   = BooleanField(default=False)
    use_penta = BooleanField(default=True)

    # All status flags associated with the test
    passed      = BooleanField(default=False)
    failed      = BooleanField(default=False)
    finished    = BooleanField(default=False)
    deleted     = BooleanField(default=False)
    approved    = BooleanField(default=False)
    awaiting    = BooleanField(default=False)
    error       = BooleanField(default=False)

    # Datetime house keeping for meta data
    creation    = DateTimeField(auto_now_add=True)
    updated     = DateTimeField(auto_now=True)

    def __str__(self):
        return '{0} vs {1} @ {2}'.format(self.dev.name, self.base.name, self.dev_time_control)

    def results(self):
        return self.as_tri() if self.use_tri else self.as_penta()

    def as_tri(self):
        return (self.losses, self.draws, self.wins)

    def as_penta(self):
        return (self.LL, self.LD, self.DD, self.DW, self.WW)

    def as_nwld(self):
        return (self.games, self.wins, self.losses, self.draws)

    def workload_type_str(self):
        return {'SPSA' : 'tune', 'DATAGEN' : 'datagen'}.get(self.test_mode, 'test')

class LogEvent(Model):

    author     = CharField(max_length=128) # Username for the OpenBench Profile
    summary    = CharField(max_length=128) # Quick summary of the Event or Error
    log_file   = CharField(max_length=128) # .log file stored in /Media/

    machine_id = IntegerField(default=0)   # Only set for Client based Log Events
    test_id    = IntegerField(default=0)   # Should always be set

    created    = DateTimeField(auto_now_add=True)

    def __str__(self):
        return "{0} {1} {2}".format(self.author, str(self.test_id), self.summary)

class Network(Model):

    default     = BooleanField(default=False)
    was_default = BooleanField(default=False)
    sha256      = CharField(max_length=8)
    name        = CharField(max_length=64)
    engine      = CharField(max_length=64)
    author      = CharField(max_length=64)
    created     = DateTimeField(auto_now_add=True)

    def __str__(self):
        return '[{}] {} ({})'.format(self.engine, self.name, self.sha256)

class NetworkAuxFile(Model):

    # Auxiliary files travel with a Network (eg YaneuraOu's progress.bin,
    # or a eval_options.txt with per-eval mandatory settings). Stored in
    # /Media/ under their own hash; workers stage every one of them into
    # the same directory as the network file, under its original name
    network = ForeignKey(Network, on_delete=CASCADE, related_name='aux_files')
    name    = CharField(max_length=64)
    sha256  = CharField(max_length=8)

    class Meta:
        unique_together = ('network', 'name')

    def __str__(self):
        return '[{}] {} aux {} ({})'.format(
            self.network.engine, self.network.name, self.name, self.sha256)

class PGN(Model):

    test_id    = IntegerField(default=0)
    result_id  = IntegerField(default=0)
    book_index = IntegerField(default=0)
    processed  = BooleanField(default=False)

    def __str__(self):
        return self.filename()

    def filename(self):
        return '%s.%s.%s.pgn.bz2' % (self.test_id, self.result_id, self.book_index)
