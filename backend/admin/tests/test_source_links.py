from backend.admin.source_links import source_links


def test_links_from_sources_yaml():
    links = source_links()
    assert links["rules"][0]["url"] == "https://yz.kyungnam.ac.kr/rule/"
    assert len(links["academic_guides"]) >= 10
    assert all(x["url"].startswith("https://www.kyungnam.ac.kr/") for x in links["notices"])
    assert "phonebook" not in links
