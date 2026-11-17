from app.suggestions import suggest_exclusions


def test_words_of_hidden_titles_are_suggested():
    hidden = ["Console Wii blanche", "Wii + 2 manettes", "Nintendo Wii Sports", "GameCube violette",
              "GameCube + jeux"]
    kept = ["Game Boy Player avec disque", "GameBoy Player GameCube", "Gameboy player complet"]
    words = [s["word"] for s in suggest_exclusions(hidden, kept, known=["gameboy player"])]
    assert words[0] == "wii"
    assert "gamecube" not in words  # also in a kept title, more than a third as often
    assert "player" not in words


def test_nothing_before_enough_hidden_listings():
    assert suggest_exclusions(["Wii", "Wii U"], []) == []


def test_known_words_stopwords_and_numbers_are_skipped():
    hidden = ["Wii très bon état 2008", "Wii bon état 2008", "Wii etat neuf 2008"]
    out = suggest_exclusions(hidden, [], known=["wii"])
    assert out == []


def test_word_common_in_kept_titles_is_not_suggested():
    hidden = ["Switch rouge", "Switch bleue", "Switch verte"]
    kept = ["Switch OLED", "Switch Lite", "Switch V2"]
    assert suggest_exclusions(hidden, kept) == []
