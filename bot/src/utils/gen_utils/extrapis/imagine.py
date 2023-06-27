from .tempimaginepy import AsyncImagine, Style, Ratio
import uuid
import io
import secrets

async def main(prompt, style, ratio, seed=None, negative=None):
    style = Style.__members__[style]
    imagine = AsyncImagine(style=style)
    try:
        if seed == None:
            seed = secrets.randbelow(10**16)
            
        img_data = await imagine.sdprem(
            prompt = prompt,
            negative = negative,
            style = style,
            ratio = Ratio.__members__[ratio],
            high_res_results = 1,
            seed =  seed,
            priority = 1,
            cfg = 15.9,
            steps = 300
        )
        await imagine.close()
    except Exception as e:
        raise BufferError(f"error imagine.py: {e}")
    
    try:    
        imagine = AsyncImagine(style=style)
        img_data = await imagine.upscale(image=img_data)
        
        await imagine.close()
        
        if img_data == None:
            raise FileNotFoundError("no files")
            
    except Exception as e:
        raise BufferError(f"error imagine.py: {e}")
        
    img_io = io.BytesIO(img_data)
    img_io.name = f"{uuid.uuid4()}.png"
    return img_io, seed