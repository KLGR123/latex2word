from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls

from latex2mathml.converter import convert as latex_to_mathml
from mathml2omml import convert as mathml_to_omml

latex = r"\frac{a}{b} + \sqrt{c}"
mathml = latex_to_mathml(latex)
omml = mathml_to_omml(mathml) 

print("LaTeX:", latex)
print("MathML:", mathml)
print("OMML:", omml)