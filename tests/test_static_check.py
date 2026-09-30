import pytest

from void.brain.scripted import BEACON_GADGET, BEACON_TESTS
from void.sandbox.static_check import check

BYPASSES = [
    "import random\nrandom._os.popen('id')\n",
    "import collections\ncollections._sys.modules['os']\n",
    "import dataclasses\ndataclasses.builtins.eval('1')\n",
    "import dataclasses\ndataclasses.sys\n",
    "import typing\ntyping.sys\n",
    "import functools\nfunctools._thread\n",
    "import os\n",
    "from os import path\n",
    "x = ().__class__.__bases__[0].__subclasses__()\n",
    "getattr(__builtins__, 'eval')\n",
    "def f():\n    return open('/etc/passwd')\n",
    "import math\nmath.__dict__\n",
    "import json\njson['x']\n",
    "class A(dict): pass\n",
    "global x\n",
    "import random as r\nr.os\n",
    "def describe():\n    return vars()\n",
    "async def f(): pass\n",
    "with open('x') as f: pass\n",
    "exec('1')\n",
    "x = lambda: __import__('os')\n",
]


@pytest.mark.parametrize("src", BYPASSES)
def test_known_bypasses_are_rejected(src):
    r = check(src)
    assert not r.ok, src


def test_valid_gadget_passes():
    assert check(BEACON_GADGET).ok
    assert not check(BEACON_TESTS).ok  # gadget import is only legal in test code
    assert check(BEACON_TESTS, allow_gadget=True).ok
    ok = check("import math\nfrom random import uniform\ndef describe():\n    return {'render': {'shape': 'cube'}}\ndef run(p):\n    return {'value': math.sqrt(abs(uniform(0, 1)))}\n")
    assert ok.ok and "math" in ok.imports


def test_size_and_syntax():
    assert not check("x = " * 5000).ok
    assert not check("def (:\n").ok
    assert not check("x = 1\x00").ok
