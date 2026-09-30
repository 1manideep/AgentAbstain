from void.ids import IdFactory, slugify
from void.memory.embed import HashingEmbedder, cosine, pack, unpack
from void.rng import RNG, derive_seed


def test_rng_streams_are_stable_and_independent():
    r = RNG(7)
    assert r.stream("kernel", 1).random() == RNG(7).stream("kernel", 1).random()
    assert r.stream("kernel", 1).random() != r.stream("kernel", 2).random()
    assert r.stream("kernel", 1).random() != r.stream("brain", 1).random()
    assert derive_seed(1, "a") != derive_seed(2, "a")


def test_ids_are_sortable_and_restorable():
    f = IdFactory("run")
    a, b = f.new("ag"), f.new("ag")
    assert a < b and f.count == 2
    g = IdFactory("run")
    g.restore(2)
    assert g.new("ag") == IdFactory("run", start=2).new("ag")
    assert slugify("  Hello, World!! ") == "hello-world"
    assert slugify("!!!") == "note"


def test_embedder_similarity_orders_sensibly():
    e = HashingEmbedder(256)
    a = e.embed("the eastern node is thin and gives little food")
    b = e.embed("eastern node thin, little food today")
    c = e.embed("Bao proposed a beacon gadget near the river")
    assert cosine(a, b) > cosine(a, c)
    assert abs(cosine(a, a) - 1.0) < 1e-9
    assert unpack(pack(a)) == [float(struct_round) for struct_round in unpack(pack(a))]
    assert len(pack(a)) == 256 * 4
    assert e.embed("") == [0.0] * 256
