import pymupdf
import pytest


@pytest.fixture(scope="session")
def paper_pdf() -> bytes:
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
    p2 = doc.new_page(width=612, height=792)
    p2.insert_text((72, 80), "II. THE LENET-5 ARCHITECTURE", fontsize=12)
    p2.insert_text(
        (72, 110), "Layer C3 is a convolutional layer with 16 feature maps.", fontsize=10
    )
    p2.insert_text(
        (72, 130), "The learning rate is set to 0.001 for the first two epochs.", fontsize=10
    )
    p3 = doc.new_page(width=612, height=792)
    p3.insert_text((72, 80), "III. RESULTS AND COMPARISONS", fontsize=12)
    p3.insert_text((72, 110), "The error rate on the MNIST test set is 0.8 percent.", fontsize=10)
    data = doc.tobytes()
    doc.close()
    return data
