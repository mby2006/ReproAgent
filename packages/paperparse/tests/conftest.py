import pymupdf
import pytest


def _build_paper() -> bytes:
    doc = pymupdf.open()

    p1 = doc.new_page(width=612, height=792)
    p1.insert_text((72, 80), "Gradient-Based Learning Applied to Document Recognition", fontsize=18)
    p1.insert_text(
        (72, 110), "Yann LeCun, Leon Bottou, Yoshua Bengio, Patrick Haffner", fontsize=11
    )
    p1.insert_text((72, 160), "ABSTRACT", fontsize=12)
    p1.insert_text(
        (72, 180),
        "We present LeNet-5, a convolutional network for handwritten character recognition.",
        fontsize=10,
    )
    p1.insert_text((72, 200), "The network is trained with gradient-based learning.", fontsize=10)

    p2 = doc.new_page(width=612, height=792)
    p2.insert_text((72, 80), "II. THE LENET-5 ARCHITECTURE", fontsize=12)
    p2.insert_text(
        (72, 110), "Layer C3 is a convolutional layer with 16 feature maps.", fontsize=10
    )
    p2.insert_text((72, 130), "The input is a 32x32 pixel image.", fontsize=10)
    p2.insert_text(
        (72, 150), "The learning rate is set to 0.001 for the first two epochs.", fontsize=10
    )

    p3 = doc.new_page(width=612, height=792)
    p3.insert_text((72, 80), "III. RESULTS AND COMPARISONS", fontsize=12)
    p3.insert_text((72, 110), "The error rate on the MNIST test set is 0.8 percent.", fontsize=10)
    p3.insert_text((72, 290), "TABLE I: Error rates on MNIST.", fontsize=10)
    x0, y0, cw, rh = 72, 300, 150, 30
    for r in range(4):
        p3.draw_line((x0, y0 + r * rh), (x0 + 2 * cw, y0 + r * rh))
    for c in range(3):
        p3.draw_line((x0 + c * cw, y0), (x0 + c * cw, y0 + 3 * rh))
    rows = [["Method", "Error"], ["LeNet-5", "0.8"], ["SVM", "1.1"]]
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            p3.insert_text((x0 + c * cw + 5, y0 + r * rh + 20), cell, fontsize=10)

    data = doc.tobytes()
    doc.close()
    return data


def _build_scanned() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 60))
    pixmap.clear_with(128)
    page.insert_image(pymupdf.Rect(72, 72, 300, 300), pixmap=pixmap)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture(scope="session")
def paper_pdf() -> bytes:
    return _build_paper()


@pytest.fixture(scope="session")
def scanned_pdf() -> bytes:
    return _build_scanned()
