# 电脑怎样认出手写数字？一次 LeNet-5 小实验

想象一张手写支票。两个人写的“5”可能很不一样，金额里的两个数字还可能连在一起。1998 年，LeCun 等人研究了怎样让电脑从样例中学会识别笔迹，并把它用于读取支票这样的任务。

这个公开案例重做了论文中的一小部分。每张图都已经单独截好，里面只有一个数字。给简化的 LeNet-5 一张训练时没见过的图，让它在 0 到 9 中选一个答案，它能认对多少？

## 模型怎样学会这件事？

1. **看带答案的图。** 模型先猜图片里的数字，再根据正确答案调整内部参数。把所有训练图片看完一遍，叫作一轮。
2. **从笔画认数字。** 模型在图片的不同位置寻找短线、弯折等局部线索，再把线索组合起来判断整个数字。
3. **换一批图考试。** 考试时模型停止调整参数。逐张记录它的预测，再和图片的标签核对。

## 两次考试，各认错了多少？

这次给模型 1,024 张测试图片，它认对 909 张，认错 115 张。

| 比较项 | 论文无形变组 | 本次实验 |
|---|---:|---:|
| 训练图片 | 60,000 张 | 4,096 张 |
| 每张练几遍 | 20 遍 | 4 遍 |
| 测试图片 | 10,000 张 | 1,024 张 |
| 每 100 张约认错 | 1 张（0.95%） | 11 张（11.23%） |

**为什么用 0.95% 比？** 原作者还试过把图片拉伸、倾斜，给模型增加新的练习材料，那一组的错误率是 0.8%。本次只用了原始训练图片，所以表中选取无形变组。本次错误率高出约 10.28 个百分点。[论文第 11 页，图 5 及正文](https://princetonuniversity.github.io/NEU-PSY-502/_static/pdf/Class%204/LeCunn1998.pdf#page=11)

## 多练几轮，能追上论文吗？

简化模型认出了多数测试数字。两次实验用的图片数量、训练轮数、模型结构和更新参数的方法都有差别。要单独看轮数的影响，可以用同一批图片、同一个模型，分别练 4 轮和 20 轮，再做同一套测试。这个案例只记录了 4 轮的成绩。

## 你觉得这是几？

网页按测试顺序保留了前 12 张图片。下面取其中第一张模型认错的图，按原始像素明暗缩小绘出。先猜一个数字，再向下核对答案。

~~~text
                ▒▒▒▒▒▒▓▓░░
            ▒▒██████▓▓▓▓▒▒
        ▒▒░░░░▒▒░░
      ▒▒▒▒
    ░░██
    ▒▒██▓▓░░░░
    ░░▓▓██████████▒▒
          ▒▒██▓▓██▓▓
            ██████▒▒
            ░░▓▓░░
~~~

这是第 9 张。模型猜 6，数据集标注为 5。这张图算进了本次的 115 张错题。

## 附录 · 原文依据与运行记录

| 条件 | 论文及出处 | 本次计划 | 本次观测或实现 | 怎样理解 |
|---|---|---|---|---|
| 训练样本数 | 60,000（[PDF 第 10 页](https://princetonuniversity.github.io/NEU-PSY-502/_static/pdf/Class%204/LeCunn1998.pdf#page=10)） | 4096 | 4096（metrics.json） | 只使用 MNIST 训练集的前一部分。 |
| 测试样本数 | 10,000（[PDF 第 10 页](https://princetonuniversity.github.io/NEU-PSY-502/_static/pdf/Class%204/LeCunn1998.pdf#page=10)） | 1024 | 1024（metrics.json） | 测试顺序固定，前 12 张用于互动。 |
| 模型结构 | LeNet-5，含卷积和下采样（[PDF 第 6 页](https://princetonuniversity.github.io/NEU-PSY-502/_static/pdf/Class%204/LeCunn1998.pdf#page=6)） | LeNet-5 简化实现 | 卷积、ReLU、最大池化、线性分类器（model.py） | 论文包含可训练下采样和径向基输出；本次实现采用现代简化结构。 |
| 训练轮数 | 20 轮（[PDF 第 10 页](https://princetonuniversity.github.io/NEU-PSY-502/_static/pdf/Class%204/LeCunn1998.pdf#page=10)） | 4 | 4（metrics.json） | 一轮指看完一遍训练集。论文图 5 的错误率约在第 10 轮趋稳，整次训练仍是 20 轮。本次每轮看的图片也更少。 |
| 批大小 | 每看 1 张就更新一次（[PDF 第 42 页](https://princetonuniversity.github.io/NEU-PSY-502/_static/pdf/Class%204/LeCunn1998.pdf#page=42)） | 64 | 64（config.yaml） | 附录 C 说明作者逐张调整权重。本次累积 64 张的结果才调整一次。 |
| 学习率 | 全局步长分五段递减，另按权重调整（[PDF 第 10 页](https://princetonuniversity.github.io/NEU-PSY-502/_static/pdf/Class%204/LeCunn1998.pdf#page=10)） | 0.03 | 0.03（config.yaml） | 第 1–2 轮为 0.0005，第 3–5 轮为 0.0002，第 6–8 轮为 0.0001，第 9–12 轮为 0.00005，随后为 0.00001。附录 C 说明每个权重的实际步长还会调整。本次 SGD 使用固定步长。 |
| 优化器 | 随机对角 Levenberg–Marquardt（[PDF 第 42 页](https://princetonuniversity.github.io/NEU-PSY-502/_static/pdf/Class%204/LeCunn1998.pdf#page=42)） | sgd | sgd（config.yaml） | 优化器决定模型每次怎样改正错误。原方法为各个权重分别计算步长，本次用普通随机梯度下降（SGD）。 |
| 损失函数 | 第 II.C 节讨论两种评分方式，图 5 这一组的具体选用未确认（[PDF 第 9 页](https://princetonuniversity.github.io/NEU-PSY-502/_static/pdf/Class%204/LeCunn1998.pdf#page=9)） | cross entropy | cross entropy（config.yaml） | 原文给出均方误差及带类别竞争项的准则。本次代码用交叉熵给预测的错误程度打分。 |
| 测试错误率 | 0.95%（无形变训练）（[PDF 第 11 页](https://princetonuniversity.github.io/NEU-PSY-502/_static/pdf/Class%204/LeCunn1998.pdf#page=11)） | error_rate | 11.23%（metrics.json） | 训练规模、模型细节与测试集不同；单次运行无法归因。 |

归档 ID（运行标识）01993dd36ff17102；模型 SHA-256 是模型文件指纹，两者不是同一概念。本次运行完成于 2026-11-21T08:02:35.867905+00:00。

来源：LeCun 等，Gradient-Based Learning Applied to Document Recognition (1998)，DOI 10.1109/5.726791。配置、模型及指标文件与案例同时提供。
