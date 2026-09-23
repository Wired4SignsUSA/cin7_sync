from app_pages import training_videos as tv


def test_registered_videos_exist_on_disk():
    # Every registry row must point at a real file, or the toggle silently vanishes.
    assert tv.available_videos() == list(tv.VIDEOS)


def test_unknown_page_has_no_video():
    assert tv._path("Not A Page") is None


def test_registry_pages_are_real_pages():
    from app_config import PAGE_OPTIONS
    assert set(tv.VIDEOS) <= set(PAGE_OPTIONS)
