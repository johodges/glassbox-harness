"""The tool registry: the harness offers the LLM whatever is listed here."""

from .code import EditCodeTool, RunScriptTool
from .coding import (CheckCodeTool, CreateFileTool, ReviewCodeTool, RunTestsTool,
                     SearchCodeTool, WriteCodeTool)
from .combine import CombineTool
from .database import QueryDatabaseTool
from .digitize import DigitizePlotTool
from .excel import UpdateExcelTool
from .files import ReadFileTool, SearchReferencesTool
from .fire_calcs import FireCalculationTool
from .image import ImageTool
from .latex import UpdateLatexTool
from .model3d import Model3DTool
from .music import MusicTool
from .office import UpdatePowerPointTool, UpdateWordTool
from .presentation import ConvertPresentationTool, ExportPdfTool
from .speech import SpeechTool
from .subagent import AskSubagentTool
from .video import VideoTool
from .vision import LookAtImageTool


def all_tools():
    media = (ImageTool(), SpeechTool(), MusicTool(), VideoTool(), Model3DTool(), CombineTool())
    documents = (ReadFileTool(), SearchReferencesTool(), UpdatePowerPointTool(), UpdateWordTool(), UpdateLatexTool(),
                 EditCodeTool(), RunScriptTool(), ConvertPresentationTool(), ExportPdfTool())
    look = LookAtImageTool()  # a vision-language model: images in, answers out
    seeing = (look, DigitizePlotTool(look))  # digitize_plot measures with Python and reads labels with look's VLM
    # knowledge or limits of their own: correlations in code, a read-only database, a second context, Excel's sums
    more = (FireCalculationTool(), QueryDatabaseTool(), AskSubagentTool(), UpdateExcelTool())
    # reviewing and developing code: tests, a linter, a search, new code, and a review the harness checks
    code = (RunTestsTool(), CheckCodeTool(), SearchCodeTool(), WriteCodeTool(), CreateFileTool(), ReviewCodeTool())
    return {t.name: t for t in media + documents + seeing + more + code}
