#!/usr/bin/env python3
"""Collect changed Python source for review; NEVER update a source contract.

Only local files are read. Optional explicit Python sources and their static
local imports can be collected; unchanged reviewed dependencies are hash-only. No HTTP requests, subprocesses, application imports,
process-environment reads, credential-file reads, package installs, or service
signals are performed. Output is an analysis-only, best-effort-redacted bundle
under <root>/ops/local-llm-path-benchmark/diagnostics/source-delta-<UTC>/.

A .py.txt analysis copy is NOT deployable. Inspect output before sharing: this
small sanitizer is not a complete secret/PII detector. Unknown plain-text secrets
can still require manual redaction. Original bytes remain in memory only.
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tarfile
import tokenize
import uuid

DEFAULT_ROOT = '/var/lib/jenkins/tomod'
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
MAX_PATHS = 128


# Metadata from the supplied snapshot; this is NOT a new runtime source contract.
# Used only to avoid copying unchanged dependency source into the analysis archive.
REVIEWED_DEPENDENCY_SNAPSHOT = 'blink-server-state-20260924-223445'
REVIEWED_DEPENDENCY_SHA256 = {
    "agents/knowledge/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "agents/knowledge/knowledge_agent.py": "03ef07fc2a318e3ba894fa1244e9961c3e91d1001f9e0ccce92d34ef1fcfd3fe",
    "agents/knowledge/knowledge_sub_agent.py": "ea52ef5c0f55f5af5312a1ea4446e38483e48454e5526fd87b9a976adb702aa2",
    "agents/product/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "agents/product/product_agent.py": "f4257655be54c7736cdb7685985818c352f73f85932dec70275ea290323d79b6",
    "agents/root/orchestrator.py": "86cdff8f69fdcfb85d4301b30a8bd0d8f58244cf931b0d2dca8d53523ae3b9dc",
    "agents/skin_tracking/bootstrap.py": "afbc89530261e91eb29ec3f58d638b13ea0c9392de33e0487cf19cca60909467",
    "agents/skin_tracking/composition.py": "350d4761b59d217279a9c1a9422ed907dcb91023bf80724fdbef13e31805551c",
    "agents/skin_tracking/extraction.py": "801dc13d5dce2a2b032dacf86e19e51916ebb76405906fcfbfc78fee84df267c",
    "agents/skin_tracking/fastapi_integration.py": "9218ff96562a8b52acec216e1daef28bcbf883bbbb0f64400e38ca7cf7e7f5d1",
    "agents/skin_tracking/http_api.py": "b53ba011f5f48cb13f4aefcd7ca0647c380dc9dff8b187d685b7189f0f29571a",
    "agents/skin_tracking/model_adapters.py": "08c9d3439765a1fd0e10f19c6efd914eb88a818dfece9cc6057d8862e4eea0c6",
    "agents/skin_tracking/observation_taxonomy.py": "e7fe335b2575534318bffce9b0bb61af95dda7bbd3a23bec6f526290c25fb858",
    "agents/skin_tracking/prompts.py": "380c2cea22030ee4f7aef4a2fd468bff27190758ca3448e1486513e1a8e57880",
    "agents/skin_tracking/service.py": "47a06c02d42b1ba00bd0729ebcbe90268c94a53f39a59ea607fd0d8c83d9fdab",
    "agents/skin_tracking/session_store.py": "8e997b7acb64723b996019a625cbf02a2899bdb589aadca34191ce8a3f84abf9",
    "agents/skin_tracking/state.py": "939fd13c67929cb0f2f43c5a8b202120c74d83ad2eaa6a87bdb26806233baa5e",
    "agents/skin_tracking/tracking_agent.py": "7562afa6989ca6a196474be11fcb35378d68af756cdd312a5e1e406a3ce3e055",
    "api/api_server.py": "e45bfa31875cffef830772e1b1decd0c6d4a41c6ba213c59b21ce903bae19de7",
    "api/profile_api.py": "61f1ced1caf3d592d07de7621ab22f338207b89f606da33020c7843a2b0b5b97",
    "app/__init__.py": "0cfd16c2b1bb4aa6179502488b567a3d3dca84195c69df102f57f797417dd70a",
    "app/api/__init__.py": "c5e79b75137811726e30acef2fa5cfc649514fce1c86da771535b368f43238c0",
    "app/api/routes/__init__.py": "c4d6ca02381f4e91206c931ce77bb2a2f3fffe7366606f45a688bddfd6d27cce",
    "app/api/routes/admin.py": "d31bf8b49dd2b94c46eb4d30dbd6a33205778799688898731861ce9beff0a498",
    "app/api/routes/auth.py": "73579b13376c2da6742dcaeb1d9a37fddde8a8db3d211180661cf33cb7f0116a",
    "app/api/routes/daily_content.py": "25f899fd7190254593261f76fe5a482f4f48b63f2f8b54966bcd639d749192bf",
    "app/api/routes/email_registration.py": "28884b6c44225ca15076fbf96e86152bcb38c4dfc754a57379edb54d0d63ab99",
    "app/api/routes/health.py": "6650ade314e3eedbc98d389374dd4d61180837d700e7c3a5a833312dbec3bb3e",
    "app/api/routes/password_recovery.py": "fffa8f32baf5a5c2c878f6d2122d39056e767716d022b1c6799b8b5846d2fc40",
    "app/api/routes/quiz.py": "efb24bf9aeb3e12811d3ad2744849021ab2e40ab95d03371f27b4188dd6951fb",
    "app/api/schemas/__init__.py": "d81b0d5a685bc82aee6dfd6360f31a016abb775667f45553008e3954818f992d",
    "app/api/schemas/auth.py": "b77a1bb3ff2c687a4ac9f912d62a3b89ac308a2b5b5d68a160f4e7df679b738c",
    "app/clients/__init__.py": "c1dad57890c908df28f45448484e8ab665244e65145195a283386af7b764b38f",
    "app/clients/chat_completion_client.py": "7bc3f8cc2efc3f423acac80eaae9dacb936e4005b6a33b95a51a44b930d75793",
    "app/clients/embedding_client.py": "5c7e5922231ee02912999c44a3b50be5b2fb115e96fc81e84321e136359d8514",
    "app/clients/knowledge_response_client.py": "fdcaebb9bd76235b8025b4043a16b6da8854e60c8af0cc6b351985f516d1e6df",
    "app/repositories/knowledge_graph_repository.py": "281497e12b447fad76beb3dcb6d764f9461c9e3ddc406462f0f0486318553ae4",
    "app/security/__init__.py": "6627f4ad933f904319ce0f1c1f729093a13b901092be7f5d36c63cf2643d27af",
    "app/security/chat_guard.py": "08b9c6947db33ed3b9f067734cea0a99ddfb0d01879732fa126939ab132c4410",
    "app/security/conversation_limit.py": "d5b6b269c53733f0854d650396adff01ef9e4ca1b400015007510a357356f85e",
    "app/security/request_guard.py": "d3a7630d04bd205a3c234c586730887f476edfe4ebb702ce79a99cf63b2f2bc2",
    "app/services/knowledge_history_service.py": "3837f04dce298640e79cc0fe6a517e80523f78c4b23948b12646c0a5c4b90396",
    "app/services/knowledge_prompt_builder.py": "0e1ce0d296d5b034d963b9748a680289247474e7d3f6df5b0e5d437ea1e5b268",
    "app/services/knowledge_retrieval_service.py": "dccab8cb6184279751daf8de1ed6ba2e345d0aa22ef2249bf7eb60adba9e0f8c",
    "apply_category_fix.py": "6a25f6cceef680164be336d59b22a0cd76d470b047ecc9f9fcd52636f882109d",
    "chatgpt/openai_client.py": "dd350ffa58cf03a3a87ecfd09d2326bb1880d1dd639ff79850b6a4fcbd2b4a36",
    "core/__init__.py": "f1a5abe35228aa4a9da8fe980a382d23c4693efc31a8d6e25c046caee0cc65d1",
    "core/admission.py": "ea09bca0ba0eca205eb13f718cc1d44f7f14abe7acba025fe160a429354a1e3a",
    "core/knowledge_response_config.py": "41040cee4c6aafa61ac266b05a7c91af9e14b5bc51f1bee8c0caf6ed2820a8ab",
    "core/observability.py": "173e04826358cfaa60ce772d92ff275bbddeb573398e622846f06f32bc744a7b",
    "core/tracing.py": "18a1576b8cca0ad6e07938abaa2ac6751bbca6265933eadb77ffde0d09ce3edf",
    "core/usage_tracking.py": "a7aaba860ccbe7c999606a43b918107ec42b93b3781184475ef61c40a0f97247",
    "database/check_partner.py": "7a782c516f89fdcd12ed3a615145f068b546dd4e4bdd547a2c1675fb2922d0fc",
    "database/fix_is_partner.py": "1a1f35695d6199b7d806f1c6c984232b68fc6f128a2d6d671aa13f1049d22257",
    "database/postgres/__init__.py": "93ba1f969ac941169ffcece8232e08132bcf55ee764e029e16b5755291316457",
    "database/postgres/connection.py": "6e258c2a9e239239c138ec896f71e4573ef0728070acef0996a0e56414bc5960",
    "database/postgres/import_taiwan_lexicon.py": "8b0c6f8d07543f595be031fac6021dca1d1ad5a3c5978f3c3a49df1591683d69",
    "database/postgres/repositories/__init__.py": "25a8f68d0434ae62aa33b49e5fab0936f2359a9fa82af6c318dcf9e3f3cbba72",
    "database/postgres/repositories/checkin.py": "3eba0aa72af6015165cfa36d9cb0ec7c6504af00d988a2bb687e201ef11a5dee",
    "database/postgres/repositories/conversation.py": "62a0f430200b1a4324fa8c9ab6a390102ed68f3cd920ab82021f327c7b81279c",
    "database/postgres/repositories/daily_content.py": "55fe607b42ba6222d6a000fc43a370fef9c9c758c9124a286cd37a05d062d327",
    "database/postgres/repositories/email_registration.py": "eb59c286e4b355d957d797e4c0aa86310342b808f3d7f1bb235be818e486bc4a",
    "database/postgres/repositories/identity.py": "b8ebfc0880a8e4300eaab819394675c1937fe37192c13825498970be16073428",
    "database/postgres/repositories/oauth_identity.py": "00a1492170561cebbecd81144c1f8b02a19503bea145d11d936379aec72a4a6d",
    "database/postgres/repositories/password_auth.py": "09acb8fc08b7d1849c2820727fcd20ab9cfc96bb5768e855d3a33fe83114b371",
    "database/postgres/repositories/password_reset.py": "839ab4efd0c107d11938a35c6f082a2cab5667d5c95bfe8e233a4b453fc2b2ab",
    "database/postgres/repositories/profile.py": "1b129995575caf08a5f53acb40048283d33a73fc9813a058e8821e1c9ba11601",
    "database/postgres/repositories/quiz.py": "ee565bc498e5e30d5dca3faefdf1129e8849a950c0ef8a431b0dd4848d1992c1",
    "database/postgres/repositories/skin_record.py": "98fe9e54d5f1cb0d4a60288a0c7924482c220dd25e2418150af0cbcb13505e04",
    "database/profile_repository.py": "dfd0bb49d400af5606b084007e16565855bdb59f5ea32e1c9501efd9e71f3a04",
    "database/repositories/__init__.py": "076fdab2502d5b6cba25f3ba350f8e1b941b0b62a4208ed8faf6afa02f51fce8",
    "database/repositories/email_registration.py": "7fd29ce563ea1b8f524266374bd76db42ab991a83d0b91cfce8dcb16d4a4f25f",
    "database/repositories/identity.py": "38c8a8f61a157ec042df5b06aee94ff26f7e9d28faf708dfca35a9747124e8f4",
    "database/repositories/oauth_identity.py": "65957879200a4caf0cbc073419c2458850e321c8fdded9dd943d2be941cb9749",
    "database/repositories/password_auth.py": "30d40eabd9f24757571634a18f7c33d07d631365513f7aa42d4244325a45472a",
    "database/repositories/password_reset.py": "a106f1e9eb25054bb39cb8cb201d3e224fb14bd295e42ecedea975c08753e854",
    "database/storage_settings.py": "327725c661f60d68714b415c0491e108f67bbc5a9ebe59322e96edb20b73e7c9",
    "database/update_faq_embeddings.py": "48f9539b2f9d97b8ce5f55308fe12b931a4c6be2924d888fe748e7b1a7645f94",
    "models/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "models/taide_client.py": "8a32f77fd76afccdb67f048feef5386157cdf456b9080ecbf2174368385a4dde",
    "schemas/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "schemas/intent.py": "c4dd69e2f5d562d7a72f64ed84be035273cf4157ac8d4aad5c17b06ed29a4503",
    "schemas/tracking_schema.py": "f032a67bf629ff3a339a27391119614a9d27859e7650764b63fcadd808497145",
    "schemas/user_analysis.py": "c7dc1bb5127cec2d4babf366ce2ba9f739a4d141a5b517fc73281174af919159",
    "scripts/ai_stack_inventory.py": "632989c31e42d0bd5afdf6721e22c62f359dca4da656fcfdd0a4a9d57d445d92",
    "scripts/apply_daily_contents_migration.py": "2750a99a46053f61fd67b2d2654ed53e50a767d4113b388cd9fe487a0a51eff3",
    "scripts/audit_admin_identity.py": "774ab6144034af8f039bb2d70e1c1e86a51e5050eb1df0ce2f232e6158c9f685",
    "scripts/audit_beauty_advice_neo4j.py": "cd42baeefddbe4d8b166be5a04b4f76d5f4684b62fe5c31fe0435bfeb2ff5524",
    "scripts/audit_data_boundaries.py": "e7531d0a735c43c677647388cb15807ef1f83c2c14394ea13fbdc6ffead8ebf3",
    "scripts/audit_faq_entries.py": "20a847cffcd2eedb44be4563d50cb1bf73413ebc8abf3d2ce777b2e713493323",
    "scripts/audit_hardcoded_secrets.py": "18b5ed2dbad7f002ce0a75b736201e8923a54841f40daa10233d0906621c1eae",
    "scripts/backfill_product_embeddings.py": "63eadc1dec538050efae0d813b05b15e65899b03664d0b853dcf6c50ec5349f3",
    "scripts/check_password_recovery_routes.py": "36bb0939546b92b767f021226939d1d413484255b98c8ef37d1bae4c5ec47e8b",
    "scripts/check_password_reset_mailer.py": "0aa7a9a3e637e2ba18d883c9d4eb2f7a991d7b087f7d18e590b94005d154ed0e",
    "scripts/check_postgres_auth_adapter.py": "bf40afad73bcdadad413339188072262087d2fa23fa05f78a3ff160a49ecbb8e",
    "scripts/check_postgres_checkin.py": "fe509549d90bcf031e10885afb5a69a6b8859a6e025ed4af780b53ecb82698da",
    "scripts/check_postgres_connection.py": "296b45175b75ef037eb87d57de1d8a24759f5a4ff705892e26f573638ee57010",
    "scripts/check_postgres_conversation.py": "b15eff4797dc8ef85e786f3491cb12e2e284d3f323644b0cd92b8994052bf872",
    "scripts/check_postgres_email_registration.py": "288c1a27ff7a2060e6b3ba67c2deb635f6479524b0b4e09a30d0ff44b308f7e1",
    "scripts/check_postgres_oauth_identity.py": "ab2e89027aa95c05ba99f2dcdfc16128458cdb8ef3ca2059e050c26683a8e1d8",
    "scripts/check_postgres_password_auth.py": "f894e8c98bd22e7e771071b5bcddb19a4f2cf171a87783933e49c454adf1dac6",
    "scripts/check_postgres_password_recovery.py": "1c5f7422c46b2e71a34f04964da2f56a98783a316770a48652e84deb8c909085",
    "scripts/check_postgres_password_reset.py": "8816abe1eff697aa9a2889dd341c188258913576495bb2c29f466c29d6d011dd",
    "scripts/check_postgres_profile.py": "ee088052f215728cf4bda2b5de3eff1106067b2f59cffc9fdde5139a9d99a7de",
    "scripts/check_postgres_quiz.py": "3bdc8f09cacad606c58e79e9597cd83cbc9a2d8589741e53f79706a838785595",
    "scripts/check_postgres_repository.py": "cb2f721adc515e1bceee64cb7c8d1d0ce8f82ffb5b72df4236b0d932083f1090",
    "scripts/check_postgres_skin_record.py": "1d74299d20741553a3ba329bfe39d8ca5caecf53c5c67d5b5a11a5618cb995bd",
    "scripts/check_runtime_config.py": "7faac2c89b10d25e514ceee5b0d5def6ee42d6b5dd9ac0637356f29a1920feb9",
    "scripts/configure_dev_smtp.py": "d727c1b91c9683784f65c229edf7b40a449787ef5d443e806c0bc474c0dd8b4c",
    "scripts/confirm_live_password_recovery_test.py": "1d9c11910f18f2cde7c9d192f7cc61562a88769f8b4282a8e794f0e818a55f88",
    "scripts/firecrawl_daily_content.py": "2c81e5fffc7a5d2a98c68eceb510f247224571838dcc3f3440fc31118b782909",
    "scripts/firecrawl_weekly_scheduler.py": "e1128472b3b740e6e1d54fc3242cae348c1451853b1e8482fe32fc3347080ad7",
    "scripts/patch_password_recovery_api.py": "7f1273f347f9b8635db120a30689bd128beb5a4787460c6297a9cabc1d412997",
    "scripts/preview_user_migration.py": "1df6caa093aa20ffe1f5f5bc2ddd99d1da7715e8110c2b6e9737e8ca54495b76",
    "scripts/send_live_password_recovery_test.py": "26f90080901bba0a4db26f10f8e630a3b5520c5a251e322e88a08178698d6d72",
    "test/test_ingredient_qa.py": "2e6082caf771236e1d443421a057deb37d7d4d45e163173f5124147d907ca165",
    "test/test_intent.py": "152b391e0b77f57ea34459113a6c08030441ef2f5ab3c51ef74bb2134a3ada8a",
    "test/test_line_only_profile.py": "310d4c04c7b555de4d057d11890bc99fbf8e6983d739d914c609dc27ea676413",
    "test/test_neo4j.py": "38561e8c296cf10166e0e588fe5487393da19b1cc666091a49f507f011265908",
    "test/test_oauth_onboarding.py": "cd354091e8233a3eb35c05baf5294943909ff8bf6de42da56fe5b0bdbd9a4870",
    "test/test_password_login.py": "a66c1a420687082b6a5a2d4cb2814b545f607a401cef66b85f0bb1215f6956dd",
    "test/test_preference_profiling_tool.py": "44c1aad891156a309445484a846e0e3ff1dfb4d091c3761832878462715b763a",
    "test/test_profile_context_bridge.py": "455cfcab490d0a14e2edd567731ee149c9817540bf6c8e66684af2bd23a89dd7",
    "test/test_report_tool.py": "558730013b8d6094e1c98a849a6991e27998f99d561c6f74718f4c225237051f",
    "test/test_response_composer.py": "4c9670cbea771d6e1f320af92a2c9a38b2dc0ffdc62e8db015897fa731038a0b",
    "test/test_root_routing.py": "06c22ddeb1767fb857f033a95fa127627b303b984bf1252a8b64ef7004e8bde4",
    "test/test_skin_extraction_provider.py": "fd01a624eeef7f956e2683d7ac5f57bae1e2be3d77ba0add81f66db413c5d939",
    "test/test_skin_tracking_composition.py": "22792f587798d9c5003b1921aed9c3ac90c0541377b2197997621aa0abb2b7b8",
    "test/test_skin_tracking_http_api.py": "d5b14c8bc6f0405fc0305f17ce1cb06ecf07065b5c4ff5e71898f1758cc05eb7",
    "test/test_skin_tracking_service.py": "8b7d15582179db05cb43ffec8e844a8f0f6f4b772a762e4dce46430cb41a1d01",
    "test/test_skin_tracking_stability.py": "3cfe66ec5144090bde43975c42faac6dc3b53bdbc0b586d96737ba7a73605a59",
    "test/test_skin_zone_extraction.py": "c64c5b121820d0c2fb1df0ec224063c64dfd2bf18f33ab51d37e504b055cce85",
    "test/test_terra_semantic_extraction.py": "9c1c849319b8865780e6bdb5cf86ef2d368d240f635419429e35343117174223",
    "tests/__init__.py": "4bb67abc64e34b8228bc2d97e3c01f7bad5c4831443ffe4dba5d20cb441ce82b",
    "tests/clients/test_chat_completion_client.py": "eeed7f1aa00f273ef890202c3d017020f610b95adae6d57a3a6aa334cb372e76",
    "tests/clients/test_embedding_client.py": "340e492e24ef3745f3d8ec8524d38be13c57834d2ccac030dcd00e6b7c9de471",
    "tests/clients/test_knowledge_response_client.py": "5d2d22563043239bd8f48b9291576f6bdeb725272a2cf28f6ea36a0ec97c0c9c",
    "tests/regression/__init__.py": "e98605d293692eb00203af60eb0bd8a41168466cfedd74d6d7cdfe9fa736143b",
    "tests/regression/live_regression.py": "fac1bbb527faf2a8a47d26f3cd1348caa8422ff1884fdd5ab1f06ca06fe9eb8f",
    "tests/regression/test_chat_guard.py": "1bbf00e322ab452b7493ab6b099b82d41597bba4c827a596afd1f213e48b86cd",
    "tests/regression/test_conversation_limit.py": "e75277cc98dbdc9fbae018c82faaf09e49f7727f8133604ea6bdab248b4f4fa8",
    "tests/regression/test_observation_tag_terra_severity.py": "6684cd14db38119ebd25ad6c168b7b81735fd00872dbd7c382b1b44c6be86063",
    "tests/regression/test_product_agent_exclusions.py": "ac8f6d6f30390a15d35190af69b4923f779bc3f2144998282531411e4cb58fe9",
    "tests/regression/test_product_category_filter.py": "9eec146346fd49516fa1440cc977220d2b19a38eb9e5b57af9de17c626e2fcf4",
    "tests/regression/test_product_currency.py": "20a66f3b21921943a27267fb8ea3d9b9d975b5b154fdf116230a1f761c92686a",
    "tests/regression/test_product_pagination.py": "e693ced9bbf08b51bfd8f3168d7967f812d1976f3801bc5fd2515f45bd707adb",
    "tests/regression/test_product_route_guards.py": "f86b256f2e44fd324287d7472d9eb6788ceae00901700fbfb85d3bbdae7033bf",
    "tests/regression/test_product_web_search.py": "847bd4bc77838219b1c2d021777932c9b3cda09cd0651391df65b5af59bdcabd",
    "tests/regression/test_request_guard.py": "46ac66ca46071fced4a7ef1d26d8c8f21bc87cf61f76b089f2b96d648ce0d09f",
    "tests/regression/test_source_contracts.py": "956659b4233f1903100e055ecb8bf813884f9227d8a78cf59f51854c749ee60f",
    "tests/repositories/test_knowledge_graph_repository.py": "8b44d5087e2480a5b8c1d31cb95a009b3dfe14d4a3258f65a770e2a22cba7e62",
    "tests/services/test_knowledge_history_service.py": "f18bda4bda846e9e2ecb19bdf5ac19a2297026afd6f5f32b546e9932d20243ab",
    "tests/services/test_knowledge_prompt_builder.py": "4c79aba79c265125100a7d19d8700500b7081c199a0a90450781aa5168acac61",
    "tests/services/test_knowledge_retrieval_service.py": "4deaf216a8d037e1543cfe54fee51f399937c30df014bc00455a04905cd54902",
    "tests/test_admission.py": "0b31f63a117ecfe8d19fa1fc1f89df4d2000343ac9e04c0b4705672c61ed3191",
    "tests/test_conversation_title_fast_path.py": "0d3dcb27a0b8b14023deb3d7e4efdae1f8aabdebb5629775e9ef875bb0eb3f58",
    "tests/test_dialogue_router.py": "6cb8fbb5aff5be231a64bea055fea06f39ba781e4788e51f8ad65f4655e7ae7f",
    "tests/test_intent_fast_path.py": "369c9e3fa3c683eb76f47bf244cb103a76c9760e65398aaa0e9ea70360559f25",
    "tests/test_knowledge_ingredient_extractor.py": "72cf5b26e805876a7e626f53c367701a844f3e404eef7d2516ca057e89c21aca",
    "tests/test_knowledge_response_config.py": "bd1d80cbfccdfe77a67e32f17f5bc66e043182f7d3887475f2e44e0837569b84",
    "tests/test_knowledge_sub_agent_orchestration.py": "a01af8b9a7c981714c3f7b0f43b8f77a8c726f03b3e26cfe44c626d16de5c82b",
    "tests/test_product_copy_fast_path.py": "e3ead359a88f6f7839c959c71a10992a455fcd5c65e362341d4e93c228bb9add",
    "tests/test_product_search_performance.py": "59116b403e7798fe9c2f7f2cbfb18932ac82cec021c9c4a0eecf40d106c34c45",
    "tests/test_product_taide_client.py": "882794fe84ecfa541829432bea68261205be2351b18c4b12a961094866cc4978",
    "tests/test_root_taide_client.py": "8d09fd8fc8799868dfabca7fa021dc776908214fc2981f64aaa9e96eb9c8c088",
    "tools/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "tools/a2a/product_knowledge_gate.py": "bdb44a63d527f65d7b56805cecdff8a61e7beed690e40d5957f42914742b86fd",
    "tools/a2a/productknowledge_orchestrator.py": "34e296f2d2dd916bab0e9379c737764d6667072eb4f9c93631356b3a3c16ad2a",
    "tools/a2a/skin_product_bridge.py": "e6960e0ecd95f178587c58fd991567936e62b69ad74d493f365f89d384e705ac",
    "tools/agent/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "tools/agent/clarification_tool.py": "494b0e6d67781001d3c91ee8aedcdae279443cf358294647aef84caf776fdaf1",
    "tools/agent/copywriting_tool.py": "a6f27e83d2c101102576446f70085dcc9b2086a1c973a3adab93c78a5d611dee",
    "tools/agent/dialogue_router.py": "e14e9df6482cab828faed8626125b7a435978a4fef09383b0140bd682f9deadf",
    "tools/agent/experience_parser.py": "7510a08bae3548aec189c6556a5cfbb6988ef26c2b89f290ae10c57a80c2d2dd",
    "tools/agent/input_guard.py": "0eda59d5fe4fdfa31469f4c550e2afc1c0db95fb27640ee727eaa3a15af4a251",
    "tools/agent/intent_parser.py": "da6f62381e78a381bcc46b41a4a8d684115e46f48d3d18450f92b5f9020eab10",
    "tools/agent/response_composer.py": "b7138dc2a2cb24516828b3c438312c7fed100de146629a6d4b267e5a9577c7b7",
    "tools/agent/tracking/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "tools/agent/tracking/export_skin_context_tool.py": "627c9085ead59486d951de25aefd008854b1616816a95b104df8e5fd08786dab",
    "tools/agent/tracking/skin_extract_tool.py": "4be1ca69128b9b47e4200df7002196b370117ea4f48fd4f27fec1d7f4778deb8",
    "tools/agent/tracking/skin_record_tool.py": "a6e9955bc88648107262e88347ca4e9c11685b70da992e5a7b8d3a0f04b52a7a",
    "tools/agent/tracking/skin_report_tool.py": "70c5fb648a10e4645968d56990bf8398fa514906155fd1f6fc3546c9a3a7cb8d",
    "tools/agent/tracking/skin_save_tool.py": "fb9d76309a466ceb7024d89d19beca54fe08ee70d7ffabe884daef254186ae14",
    "tools/auth/auth_session.py": "4961428481f471e770992432fc38d70fa579d7db88859b7e2d98ae666d6ac58d",
    "tools/auth/google_login.py": "05dfbeaa3ee5d6c8b746a45658abf72a32d37d42692bd32016f20fd02a398a1c",
    "tools/auth/line_login.py": "5e583224fdaf09b5db3e3a987686a19fbae397cc4806398119d67921ec71b6f2",
    "tools/auth/oauth_onboarding.py": "b4a84564c1688e2c52f1d44bd0789af4ff295f7fe28834aff07f9d377241430c",
    "tools/auth/password_login.py": "3868f7bd41da35411b242ea2d894bb666383cd0177b35a5d7121eef2e6f19fe3",
    "tools/auth/password_reset.py": "7c0fdf7ad3f111b6aebbc3781d267a6915727ff3132bc96493dda5958b27c456",
    "tools/auth/password_reset_mailer.py": "05d14157dbcdde8a0ebcf3ef2248407415508d9a5939e136c80b7ad4f5d88b53",
    "tools/auth/postgres_auth_session.py": "1da4160925489d1429061630d248a738d2e5d5e29eccd4b91df4e4c0f4cce70e",
    "tools/auth/postgres_email_registration.py": "b8dfdb312ee5c2a7cdddf077e0cf1356e24eccb41717bebade42cc678099c93b",
    "tools/auth/postgres_password_login.py": "be50f60af0f35f50f755b5fa9264e52cd2307e7e2a1d25eb01c7eefe368f3d0b",
    "tools/auth/postgres_password_recovery.py": "0f837ad6456ed7413f5292c7aec2d8eb70e17e96058d431e39fc5f8e9bc1ef98",
    "tools/data/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "tools/data/crawl_product_brief.py": "4d7214b57554147ab42fd4f89a4fe5b4a5ce9d7424c1d1adfc3e2c3217f6489d",
    "tools/data/cyberbiz_client.py": "e1d5e4cc50106d03e7d9b9e8480be6b9412d6f988873eb0c81060678e7487ece",
    "tools/getzep/conversation_memory_service.py": "2b2f1b8cb1441ec28ff7b00db42039d8e258a442be8b5a98d148aaf993f93bd4",
    "tools/getzep/conversation_title_service.py": "7e83df64f2e4b7c3fc7df34f99c9e0a876fdfe03c1d87ee502226340ab217e4f",
    "tools/getzep/test_zep_memory.py": "1778fe92875648cebcd6e3c1fc1595f69409a8f5d3fa99214d901f7fb79ab579",
    "tools/getzep/test_zep_memory_adapter.py": "dd1bd00db638aafa1a70c4632143db68b916f58a26868833c2bc919b47590c4d",
    "tools/getzep/zep_connection.py": "3bdc6a1ce52d681ee34f138d7d4f8647cd76f29d3f3a2f1cd4c5ec0e0872dfb0",
    "tools/getzep/zep_memory_adapter.py": "5f17049933136ec032d45ea971ecd409697e4cfd83a84c26655f337cf70d93de",
    "tools/search/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "tools/search/graph_search.py": "fbbe34723cc77b79fccee42f6babd635bfb906c797f3826f7e17101a5cb7bc0c",
    "tools/search/ingredient_extractor.py": "fc2447f154a59ca4975ee8fb74fbc716821c6aff207e83c2c6ce3f3219f0e227",
    "tools/search/ingredient_tool.py": "d208de2ae5e5ac82ab7a4e7223a0fedcc48cb42a3eeb9c91c1fb8b06210809eb",
    "tools/search/knowledge_intent.py": "0f4186955e94a7f340a1c11535d1519d40d156fa3cfc06ba51ccdff443eabbf2",
    "tools/search/official_source_search.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "tools/search/product_web_search.py": "80dd9ca72dea290e50e44d177470cdf67f0e442a3a4e71960dcd949bda5c65c8",
    "tools/search/risk_rule_tool.py": "4f5e52be6d691b5aa2308b4860fa8c435aae8b4867e0da42c8ca06959215647a",
    "tools/search/skinconcern_tool.py": "b1e753c94e13ff5efc71bdb006c13f4d42ef69d1eaa6d34b4aaed35e751fd136",
    "tools/search/test_multiturn_product_knowledge.py": "f04710aa3f25083ab2e0accc5986c81c961e2b821eb4d84a0548a88e6e8dee83",
    "tools/search/test_product_knowledge_orchestrator.py": "2e41398b6cf194316fcce9895cc85e392d078031e851e91aeccdcafd299a7cb1",
    "tools/tests/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "tools/tests/check_fda.py": "1fa4d2ea64d56be7f8a917d9b382e2e7be90079a87e27609c84a28d7810073ed",
    "tools/tests/check_law.py": "8ed0ba591198345fd71347d40ae23cf60398dac8296b46705151a6d41290ed34",
    "tools/tests/check_pdf.py": "1051be7a700c9564853f648d3c5d399d0ed58880b939e997bd6f7e6904eeb8fa",
    "tools/tests/test_intent.py": "8eef6b1d2793b00fa796f4b4238251d79392f9365bd6063fbfcbd63f858348e3",
    "tools/tests/test_knowledge_agent.py": "4272be510cbea6f65df5c7cbb270d2676a9c1c6fbdcc7b56a53f6d85a6d50fed",
    "tools/tests/test_knowledge_intent.py": "544deb3dfe2dc2c05b5a6a1e069103ee50363c996de2cbfdf3707776a2a3b9f3",
    "tools/user/behavior_analysis_tool.py": "322201cbf77b051c3baf2f5d6f95ea2ff8187df6a887a09a1992f5d83282e18c",
    "tools/user/customer_type_profiling_tool.py": "9dd69243aa7463faf2e75b9b04630a704914db24e653a2b0d55cfce76a590eaa",
    "tools/user/preference_profiling_tool.py": "c6fc412fb15fc18130521e4143cc9b33c3cd467df755a561a547efc8f4eaf620",
    "tools/user/profile_context_bridge.py": "131974c7a79ac2ad5fba08afe7740bb2248c312f8d28f31d326fd53697996f9b",
    "tools/user/quiz_analysis_tool.py": "1dac84346e99169eaef006968973252507fb86167689048832c9887f3b01d89d",
    "tools/user/user_analysis_context_bridge.py": "7c1db851059ef3702ca6c3a36bdc145b391cd525a2f122da7555846cb4ef253b"
}
MAX_DEPENDENCY_FILES = 256
FIRST_PARTY_ROOTS = {'api', 'app', 'agents', 'core', 'database', 'tools', 'models', 'chatgpt'}


class Refuse(RuntimeError):
    pass


def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds')


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def relative_py(value: str) -> PurePosixPath:
    if not isinstance(value, str) or '\\' in value:
        raise Refuse('Invalid source path')
    rel = PurePosixPath(value)
    if rel.is_absolute() or not rel.parts or any(x in ('', '.', '..') for x in value.split('/')):
        raise Refuse('Source path must be relative without traversal')
    if rel.suffix != '.py' or any(x.startswith('.') for x in rel.parts):
        raise Refuse('Only non-hidden relative .py source paths are allowed')
    return rel


def checked_path(base: Path, rel: PurePosixPath) -> Path:
    current = base
    for part in rel.parts:
        current = current / part
        try:
            if current.is_symlink():
                raise Refuse('Source/config symlink refused: ' + str(rel))
        except OSError:
            raise Refuse('Unable to inspect source/config path') from None
    return current


def stat_key(s: os.stat_result) -> tuple:
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def stable_read(path: Path, limit: int) -> tuple[bytes, dict]:
    if path.is_symlink():
        raise Refuse('Refusing symlink input: ' + path.name)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise Refuse('Input is not a regular file: ' + path.name)
        if before.st_size > limit:
            raise Refuse('Input exceeds bounded size: ' + path.name)
        with os.fdopen(fd, 'rb', closefd=False) as f:
            raw = f.read(limit + 1)
        after = os.fstat(fd)
        current = path.stat(follow_symlinks=False)
        if len(raw) > limit or stat_key(before) != stat_key(after) or stat_key(after) != stat_key(current):
            raise Refuse('Input changed during read: ' + path.name)
        return raw, {'size_bytes': before.st_size, 'mtime_ns': before.st_mtime_ns,
                     'device': before.st_dev, 'inode': before.st_ino,
                     'sha256_original': digest(raw)}
    finally:
        os.close(fd)


def read_json(path: Path) -> dict:
    raw, _ = stable_read(path, MAX_JSON_BYTES)
    data = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(data, dict):
        raise Refuse('Expected a JSON object: ' + path.name)
    return data


# Names used ONLY to select literal secret values; env key/name strings are kept.
_SECRET_NAME = re.compile(
    r'(?i)(?:^|_)(?:api_?key|secret|password|passwd|pwd|credential|access_?token|'
    r'refresh_?token|id_?token|private_?key|authorization|token|dsn)(?:$|_)')


def sensitive(name: object) -> bool:
    if not isinstance(name, str):
        return False
    return bool(_SECRET_NAME.search(name.strip('_').replace('-', '_')))


def target_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name): return node.id
    if isinstance(node, ast.Attribute): return node.attr
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        return str(node.slice.value)
    return ''


def string_nodes(expr: ast.AST | None) -> list[ast.AST]:
    # Deliberately do not rewrite function names, env selectors or entire RHSs.
    if isinstance(expr, ast.Constant) and isinstance(expr.value, (str, bytes)):
        return [expr]
    if isinstance(expr, ast.JoinedStr):
        return [expr]
    if isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
        return [n for x in expr.elts for n in string_nodes(x)]
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, (ast.Add, ast.Mod)):
        return string_nodes(expr.left) + string_nodes(expr.right)
    if isinstance(expr, ast.BoolOp):
        return [n for x in expr.values for n in string_nodes(x)]
    return []


def source_text(raw: bytes) -> str:
    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    return raw.decode(encoding)


def literal_quote(raw: str) -> tuple[str, str, str] | None:
    match = re.match(r'''(?is)^([rubf]*)("""|\'\'\'|"|\')(.*)''', raw)
    if not match: return None
    prefix, quote, rest = match.groups()
    if not rest.endswith(quote): return None
    return prefix, quote, rest[:-len(quote)]


def masked_literal(raw: str) -> str:
    parts = literal_quote(raw)
    if parts is None:
        raise Refuse('Cannot safely mask a sensitive string literal')
    prefix, quote, inner = parts
    if not inner: return raw
    # Retain quote type and physical line count; remove value, not code structure.
    if len(quote) == 3:
        replacement = '<REDACTED>' + ''.join(re.findall(r'\r\n|\r|\n', inner))
    else:
        replacement = '<REDACTED>' + ''.join('\\' + x for x in re.findall(r'\r\n|\r|\n', inner))
    return prefix + quote + replacement + quote


def redact_patterns(text: str) -> str:
    text = re.sub(r'-----BEGIN [^\r\n]*PRIVATE KEY-----.*?-----END [^\r\n]*PRIVATE KEY-----',
                  lambda m: '<REDACTED_PRIVATE_KEY>' + ''.join(re.findall(r'\r\n|\r|\n', m.group())),
                  text, flags=re.S)
    text = re.sub(r'([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/\'"@]+:[^\s/\'"@]+@',
                  r'\1<REDACTED_USERINFO>@', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]{8,}', '<REDACTED_API_KEY>', text)
    text = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '<REDACTED_JWT>', text)
    text = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}', 'Bearer <REDACTED>', text)
    text = re.sub(r'(?i)([?&](?:api_key|token|access_token|password|secret)=)[^\s&\'"<>]+',
                  r'\1<REDACTED>', text)
    return text


def sanitize_python(raw: bytes) -> tuple[str, dict]:
    text = source_text(raw)
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        # Do not leak source lines through SyntaxError formatting.
        raise Refuse(f'Live Python syntax error at line {exc.lineno}; no source copy exported') from None
    lines = text.splitlines(keepends=True)
    starts = []; pos = 0
    for line in lines:
        starts.append(pos); pos += len(line)

    def ast_offset(line: int, byte_column: int) -> int:
        return starts[line - 1] + len(lines[line - 1].encode('utf-8')[:byte_column].decode('utf-8'))

    sensitive_ranges = []
    def mark(expr):
        for node in string_nodes(expr):
            if isinstance(node, ast.Constant) and not node.value: continue
            sensitive_ranges.append((ast_offset(node.lineno, node.col_offset),
                                     ast_offset(node.end_lineno, node.end_col_offset)))

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(sensitive(target_name(t)) for t in node.targets):
            mark(node.value)
        elif isinstance(node, ast.AnnAssign) and sensitive(target_name(node.target)):
            mark(node.value)
        elif isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and sensitive(k.value): mark(v)
        elif isinstance(node, ast.Call):
            for k in node.keywords:
                if sensitive(k.arg): mark(k.value)
            function = target_name(node.func)
            if function in {'getenv', 'get', 'setdefault'} and node.args:
                key = node.args[0]
                if isinstance(key, ast.Constant) and sensitive(key.value) and len(node.args) > 1:
                    mark(node.args[1])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            positional = node.args.posonlyargs + node.args.args
            for arg, default in zip(positional[-len(node.args.defaults):], node.args.defaults):
                if sensitive(arg.arg): mark(default)
            for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
                if sensitive(arg.arg): mark(default)

    edits = []; reports = []
    # Python 3.12+ tokenizers split f-strings into several token types. Handle
    # outer JoinedStr ranges directly, then skip their enclosed tokens.
    joined = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr): continue
        a = ast_offset(node.lineno, node.col_offset)
        b = ast_offset(node.end_lineno, node.end_col_offset)
        joined.append((a, b, node.lineno, node.end_lineno))
    handled = []
    for a, b, start_line, end_line in sorted(set(joined), key=lambda x:(x[0], -x[1])):
        if any(a >= left and b <= right for left,right in handled): continue
        old = text[a:b]
        if any(a >= left and b <= right for left,right in sensitive_ranges):
            new = masked_literal(old)
        else:
            new = redact_patterns(old)
        handled.append((a,b))
        if new != old:
            edits.append((a,b,new))
            reports.append({'start_line':start_line, 'end_line':end_line, 'reason':'fstring_secret_value'})
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except tokenize.TokenError:
        raise Refuse('Python tokenization failed; no source copy exported') from None
    for token in tokens:
        if token.type not in (tokenize.STRING, tokenize.COMMENT): continue
        a = starts[token.start[0] - 1] + token.start[1]
        b = starts[token.end[0] - 1] + token.end[1]
        if any(a >= left and b <= right for left,right in handled): continue
        old = text[a:b]
        if token.type == tokenize.STRING and any(a >= left and b <= right for left, right in sensitive_ranges):
            new = masked_literal(old)
            reason = 'sensitive_literal_value'
        elif token.type == tokenize.COMMENT:
            new = re.sub(r'(?i)((?:api_?key|secret|password|passwd|token)\s*[:=]\s*)[^\r\n]+',
                         r'\1<REDACTED>', old)
            new = redact_patterns(new); reason = 'comment_secret_pattern'
        else:
            new = redact_patterns(old); reason = 'credential_pattern'
        if new != old:
            edits.append((a, b, new))
            reports.append({'start_line': token.start[0], 'end_line': token.end[0], 'reason': reason})
    for a, b, value in sorted(edits, reverse=True): text = text[:a] + value + text[b:]
    if len(text.splitlines()) != len(source_text(raw).splitlines()):
        raise Refuse('Sanitization changed physical line count; no source copy exported')
    try:
        ast.parse(text)
    except SyntaxError:
        raise Refuse('Sanitized Python failed syntax check; no source copy exported') from None
    # Credential fingerprints are intentionally not individually published.
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend({'line':node.lineno, 'module':n.name} for n in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append({'line':node.lineno, 'module':'.' * node.level + (node.module or '')})
    return text, {'original_ast_parse':True, 'analysis_ast_parse':True,
                  'physical_line_count_preserved':True, 'redactions':reports,
                  'imports':sorted(imports, key=lambda x:x['line'])}


def write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('xb') as f: f.write(data)
    path.chmod(0o600)


def json_bytes(obj: object) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def summarize_contract(source: Path, critical: dict) -> list[dict]:
    result = []
    for rel, expected in sorted(critical.items()):
        p = checked_path(source, relative_py(rel))
        try:
            data, meta = stable_read(p, MAX_SOURCE_BYTES)
            result.append({'path':rel, 'expected_sha256':expected,
                           'actual_sha256':meta['sha256_original'],
                           'matches':meta['sha256_original'] == expected})
        except (OSError, Refuse) as exc:
            result.append({'path':rel, 'expected_sha256':expected, 'actual_sha256':None,
                           'matches':False, 'error_type':type(exc).__name__})
    return result


def static_local_imports(source: Path, rel: str, raw: bytes) -> tuple[list[dict], list[dict]]:
    """Resolve source paths statically, without importlib or application imports.

    A matching reviewed file is a traversal boundary, not a claim about the
    interpreter's live import graph. Dynamic imports are outside this collector.
    """
    tree = ast.parse(source_text(raw))
    rp = relative_py(rel)
    package = list(rp.parts[:-1])
    found: dict[str, dict] = {}
    issues: list[dict] = []

    def add(candidate: PurePosixPath, line: int, module: str) -> None:
        path = checked_path(source, candidate)
        if path.is_file():
            key = candidate.as_posix()
            found.setdefault(key, {'path': key, 'imported_by': rel,
                                    'line': line, 'module': module})

    def resolve(module: str, line: int, required: bool) -> None:
        parts = module.split('.') if module else []
        if not parts or parts[0] not in FIRST_PARTY_ROOTS:
            return
        if not all(p.isidentifier() for p in parts):
            issues.append({'path': rel, 'line': line, 'module': module,
                           'reason': 'Unsupported local module path'})
            return
        file_rel = PurePosixPath(*parts).with_suffix('.py')
        init_rel = PurePosixPath(*parts, '__init__.py')
        file_path = checked_path(source, file_rel)
        init_path = checked_path(source, init_rel)
        has_file, has_package = file_path.is_file(), init_path.is_file()
        if has_file and has_package:
            issues.append({'path': rel, 'line': line, 'module': module,
                           'reason': 'Ambiguous module file and package; not selecting one'})
            return
        package_dir = checked_path(source, PurePosixPath(*parts))
        if not (has_file or has_package or package_dir.is_dir()):
            if required:
                issues.append({'path': rel, 'line': line, 'module': module,
                               'reason': 'Static first-party module not found'})
            return
        # Package initializers may have side effects; collect if new/changed.
        for n in range(1, len(parts)):
            add(PurePosixPath(*parts[:n], '__init__.py'), line, module)
        if has_package:
            add(init_rel, line, module)
        elif has_file:
            add(file_rel, line, module)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                resolve(alias.name, node.lineno, True)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = len(package) - node.level + 1
                if keep < 1:
                    issues.append({'path': rel, 'line': node.lineno,
                                   'reason': 'Relative import above supported source package'})
                    continue
                parts = package[:keep] + ((node.module or '').split('.') if node.module else [])
                module = '.'.join(parts)
            else:
                module = node.module or ''
            resolve(module, node.lineno, True)
            # Imported names can be symbols, so absent child modules are not errors.
            for alias in node.names:
                if alias.name != '*' and module:
                    resolve(module + '.' + alias.name, node.lineno, False)
    return sorted(found.values(), key=lambda x: x['path']), issues


def collect(root: Path, failed_run: Path, *, include_sources: list[str] | None = None,
            follow_local_imports: bool = False) -> tuple[Path, dict]:
    root = root.resolve(strict=True)
    harness = checked_path(root, PurePosixPath('ops/local-llm-path-benchmark'))
    source = checked_path(root, PurePosixPath('cosmetic-agent'))
    if not source.is_dir() or not harness.is_dir():
        raise Refuse('Project source/harness directory missing')
    run = failed_run.resolve(strict=True)
    if run.parent != harness / 'runs' or failed_run.is_symlink():
        raise Refuse('--run-dir must be one existing public runs/<timestamp> directory')
    contract_path = checked_path(harness, PurePosixPath('source_contract.json'))
    report_path = checked_path(run, PurePosixPath('source-contract-check.json'))
    contract_raw, _ = stable_read(contract_path, MAX_JSON_BYTES)
    report_raw, _ = stable_read(report_path, MAX_JSON_BYTES)
    contract = json.loads(contract_raw.decode('utf-8-sig'))
    report = json.loads(report_raw.decode('utf-8-sig'))
    if not isinstance(contract, dict) or not isinstance(report, dict):
        raise Refuse('Expected source contract and failure report objects')
    critical = contract.get('critical_original_sha256')
    if not isinstance(critical, dict) or not (1 <= len(critical) <= MAX_PATHS):
        raise Refuse('Unsupported source contract')
    for rel, value in critical.items():
        relative_py(rel)
        if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
            raise Refuse('Source contract contains invalid SHA256')
    mismatch = report.get('mismatches')
    if report.get('pass') is not False or not isinstance(mismatch, list) or not (1 <= len(mismatch) <= MAX_PATHS):
        raise Refuse('Expected a failed source-contract report with mismatches')

    reported: dict[str, dict] = {}
    for row in mismatch:
        if not isinstance(row, dict):
            raise Refuse('Malformed mismatch entry')
        rel = row.get('path'); relative_py(rel)
        if rel not in critical or row.get('expected') != critical[rel] or rel in reported:
            raise Refuse('Failure report and installed source contract differ; no assumptions made')
        reported[rel] = {'expected': row['expected'], 'reported_actual': row.get('actual')}
    explicit = sorted(set(include_sources or []))
    if len(explicit) > MAX_PATHS:
        raise Refuse('Too many explicitly included source files')
    for rel in explicit:
        relative_py(rel)
        if PurePosixPath(rel).parts[0] not in FIRST_PARTY_ROOTS:
            raise Refuse('--include-source must be below a supported first-party package')

    started = utc()
    before = summarize_contract(source, critical)
    selected = dict(reported)
    for row in before:
        if not row['matches'] and row['path'] not in selected:
            selected[row['path']] = {'expected': row['expected_sha256'], 'reported_actual': None}
    known = {**REVIEWED_DEPENDENCY_SHA256, **critical}
    for rel in explicit:
        selected.setdefault(rel, {'expected': known.get(rel), 'reported_actual': None})
    pending = sorted(selected)
    queued = set(pending)
    checked: dict[str, dict] = {}
    failed_paths: set[str] = set()
    analysis: dict[str, bytes] = {}
    metadata: list[dict] = []
    errors: list[dict] = []
    edges: list[dict] = []
    statically_unresolved: list[dict] = []
    while pending:
        rel = pending.pop(0)
        if len(checked) + len(failed_paths) >= MAX_DEPENDENCY_FILES:
            errors.append({'reason': 'Static dependency file limit reached',
                           'limit': MAX_DEPENDENCY_FILES, 'remaining_paths': pending + [rel]})
            break
        try:
            raw, provenance = stable_read(checked_path(source, relative_py(rel)), MAX_SOURCE_BYTES)
            expected = known.get(rel)
            must_export = rel in selected or provenance['sha256_original'] != expected
            checked[rel] = {'path': rel, 'expected_sha256': expected,
                            'actual_sha256': provenance['sha256_original'],
                            'matches_reviewed_hash': expected is not None and provenance['sha256_original'] == expected,
                            'exported': False,
                            'dependency_not_in_runtime_contract': rel not in critical}
            if must_export:
                clean, detail = sanitize_python(raw)
                staged = 'source/' + rel + '.txt'
                analysis[staged] = clean.encode('utf-8')
                previous = selected.get(rel, {'expected': expected, 'reported_actual': None})
                metadata.append({'path': rel, 'analysis_path': staged, **provenance,
                                 **previous, **detail, 'sha256_analysis': digest(analysis[staged]),
                                 'same_as_failed_run_hash': (provenance['sha256_original'] == previous['reported_actual'])
                                      if previous['reported_actual'] else None,
                                 'collection_reason': 'failure_or_live_contract_drift' if rel in critical and rel in selected
                                      else ('explicit_include' if rel in explicit else 'new_or_changed_static_dependency')})
                checked[rel]['exported'] = True
                if follow_local_imports:
                    imports, unresolved = static_local_imports(source, rel, raw)
                    edges.extend(imports)
                    statically_unresolved.extend(unresolved)
                    for item in imports:
                        dep = item['path']
                        if dep not in queued:
                            queued.add(dep); pending.append(dep)
            # Stop traversal at files matching a supplied reviewed snapshot hash.
            # This does not imply complete runtime import coverage.
        except (OSError, Refuse, UnicodeError, ValueError, SyntaxError) as exc:
            failed_paths.add(rel)
            errors.append({'path': rel, 'error_type': type(exc).__name__,
                           'reason': str(exc) if isinstance(exc, Refuse)
                                      else 'Unable to safely collect this file'})

    after = summarize_contract(source, critical)
    drift = [x['path'] for x, y in zip(before, after) if x != y]
    # Check EVERY newly inspected dependency again, including hash-only boundaries.
    for rel, item in checked.items():
        try:
            _, last = stable_read(checked_path(source, relative_py(rel)), MAX_SOURCE_BYTES)
            item['after_sha256'] = last['sha256_original']
            if item['actual_sha256'] != item['after_sha256']:
                drift.append(rel)
        except (OSError, Refuse):
            drift.append(rel); item['after_sha256'] = None
    for input_path, captured in ((contract_path, contract_raw), (report_path, report_raw)):
        try:
            if stable_read(input_path, MAX_JSON_BYTES)[0] != captured:
                drift.append('collector-input/' + input_path.name)
        except (OSError, Refuse):
            drift.append('collector-input/' + input_path.name)
    coherent = not drift
    details = {
        'started_at': started, 'finished_at': utc(), 'failed_run': run.name,
        'snapshot': contract.get('snapshot'), 'contract_file_sha256': digest(contract_raw),
        'scope': 'changed contract sources + explicit sources + optional static local imports; NOT process loaded-code proof',
        'files_checked': len(critical), 'unchanged_files_are_hash_only': True,
        'explicit_sources': explicit, 'follow_local_imports': follow_local_imports,
        'dependency_reference_snapshot': REVIEWED_DEPENDENCY_SNAPSHOT,
        'dependency_traversal': 'only selected/new/changed Python ASTs; stop at unchanged reviewed bytes; dynamic imports not executed',
        'report_mismatches': reported, 'hashes_before': before, 'hashes_after': after,
        'static_import_edges': edges,
        'dependency_checks': list(checked.values()),
        'statically_unresolved_imports': statically_unresolved,
        'changed_during_collection': sorted(set(drift)),
        'files': metadata, 'errors': errors, 'collection_consistent': coherent,
        'needs_human_secret_review': True, 'not_deployable': True,
    }
    passed = coherent and not errors and not statically_unresolved and bool(metadata)
    status = {'status': 'COLLECTED_FOR_REVIEW' if passed else 'PARTIAL_OR_CHANGED',
              'exit_code': 0 if passed else 2, 'source_files_exported': len(analysis),
              'application_modules_imported': False, 'http_requests': 0, 'service_signals': 0,
              'source_or_config_writes': False, 'source_contract_updated': False}
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    diagnostics = checked_path(harness, PurePosixPath('diagnostics'))
    diagnostics.mkdir(exist_ok=True, mode=0o700)
    out = diagnostics / ('source-delta-' + stamp + '-' + uuid.uuid4().hex[:6])
    out.mkdir(mode=0o700)
    for rel, data in analysis.items():
        write_new(out / rel, data)
    write_new(out / 'source-contract-delta.json', json_bytes(details))
    write_new(out / 'collection-status.json', json_bytes(status))
    write_new(out / 'README.txt', (
        'ANALYSIS ONLY - DO NOT DEPLOY\n'
        'Changed contract sources and explicitly requested files are exported.\n'
        'Optional static first-party imports are followed only across new/changed files;\n'
        'unchanged reviewed dependencies are hash-only boundaries. Dynamic imports are not executed.\n'
        'No application import, HTTP request, process signal, env/cookie/password-file read, git or pip command.\n'
        'Original source and source_contract.json are NOT modified.\n'
        'String-value redaction preserves Python syntax and physical line count; it is best effort.\n'
        'Review for secrets/PII before upload. Do not deploy .py.txt files.\n'
        'Original SHA256 identifies live bytes; analysis SHA256 identifies redacted copies.\n'
        'This does NOT establish which code an already-running Python process has loaded.\n'
    ).encode('utf-8'))
    sums = []
    for p in sorted(out.rglob('*')):
        if p.is_file():
            sums.append(digest(p.read_bytes()) + '  ' + p.relative_to(out).as_posix())
    write_new(out / 'SHA256SUMS.txt', ('\n'.join(sums) + '\n').encode())
    archive = out.with_name(out.name + '.tar.gz')
    with archive.open('xb') as f:
        with tarfile.open(fileobj=f, mode='w:gz') as t:
            for p in sorted(out.rglob('*')):
                if p.is_file():
                    t.add(p, arcname=out.name + '/' + p.relative_to(out).as_posix(), recursive=False)
    archive.chmod(0o600)
    write_new(Path(str(archive) + '.sha256'), (digest(archive.read_bytes()) + '  ' + archive.name + '\n').encode())
    return archive, status


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root', default=DEFAULT_ROOT)
    ap.add_argument('--run-dir', required=True, help='Failed public runs/<timestamp> folder')
    ap.add_argument('--include-source', action='append', default=[],
                    help='Additional relative first-party .py file; repeat for multiple files')
    ap.add_argument('--follow-local-imports', action='store_true',
                    help='Statically include new/changed local Python dependencies; never import code')
    args = ap.parse_args()
    os.umask(0o077)
    try:
        archive, status = collect(Path(args.root), Path(args.run_dir),
                                  include_sources=args.include_source,
                                  follow_local_imports=args.follow_local_imports)
        print('[STATUS]', status['status'])
        print('[SOURCE FILES]', status['source_files_exported'])
        print('[ARCHIVE]', archive)
        print('[NOTE] Review for secrets before upload; analysis copy is not deployable.')
        return status['exit_code']
    except (OSError, Refuse, ValueError) as exc:
        message = str(exc) if isinstance(exc, Refuse) else type(exc).__name__
        print('[STOP]', message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
