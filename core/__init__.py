"""纯逻辑子模块集合。

本包内的模块不注册任何 AstrBot 插件/LLM 工具/Web API，只提供可独立测试的
纯函数与小型工具类，供 ``main.py`` 中的插件类调用。

为什么不把插件类搬到这里
------------------------
AstrBot 的 ``Star.__init_subclass__`` 以 ``cls.__module__`` 作为 ``star_map``
的键，而 ``star_manager`` 用 ``"data.plugins.<插件目录名>.main"`` 去查找插件。
因此插件类、以及所有带 ``@filter.llm_tool()`` 装饰器的方法必须留在
``main.py``，否则插件无法被加载、LLM 工具也会丢失归属信息。
"""
