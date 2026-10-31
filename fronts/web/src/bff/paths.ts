/** Repository paths shared by the BFF modules. */

import {dirname, resolve} from "node:path";
import {fileURLToPath} from "node:url";

const here = dirname(fileURLToPath(import.meta.url));

/** agent-augury checkout root (this file lives in fronts/web/src/bff). */
export const repoRoot = resolve(here, "../../../..");
